"""Skládání překrývajících se snímků do souvislého pásu a řezání na stránky.

K čemu to je
------------
Výška obrazovky určuje strop kvality: do 2079 px se celá A4 vejde nejvýš
při 178 DPI, na Full HD dokonce jen při ~92 DPI. Chce-li uživatel ostřejší
obraz, musí v prohlížeči zvětšit zoom – pak se ale na jednu obrazovku vejde
jen část stránky a snímky je nutné poskládat zpět.

Vodorovně se nic dělit nemusí: šířky bývá dost, stránka se do okna vejde.
Skládá se tedy pouze svisle.

Jak se hledá posun
------------------
1. Z každého snímku se udělá **profil řádků** – průměrný jas každého řádku.
   Počítá se přes `resize` na šířku 1 pixel filtrem BOX, což je přesný průměr
   a běží v C (~16 ms na snímek).
2. Projdou se **všechny** celočíselné posuny a profily se porovnají na
   každém čtvrtém řádku. Podvzorkování je fázově přesné pro libovolný posun,
   na rozdíl od průměrování do hrubé mřížky – to posun, který není násobkem
   kroku, rozfázuje a správné řešení propadne v žebříčku.
3. Nalezený posun se **ověří na skutečných pixelech** v plném rozlišení.
   Samotný profil nestačí: řádky textu se opakují pravidelně,
   takže profil dobře sedí i při posunu o celý řádek. Pixelová kontrola
   rozliší stejný text od jiného textu na stejné pozici.

Zlom stránky
------------
Prohlížeče PDF nescrollují donekonečna: na konci stránky skočí na další
a sousední snímky pak nemají žádný společný obsah. Takový spoj se nesmí
odhadovat – rozpozná se podle toho, že ani při obvyklém posunu nesedí pixely,
a založí se **nová stránka**. Bez toho by se dvě různé stránky slepily
do jednoho pásu na náhodné pozici.

Prázdný překryv se odmítá: v pruhu bez textu sedí na sebe cokoli. Vyžaduje
se, aby překryv obsahoval dost řádků textu, a to na **obou** snímcích.

Vymazané oblasti se při skládání **nekreslí**, místo aby se vybarvily bíle.
Vyplnit je ze sousedního snímku jde jen tehdy, když je překryv vyšší než maska;
jinak část místa zůstane bílá.
Maska je zadaná v souřadnicích snímku, takže po složení nepadne na okraj
stránky, ale doprostřed – bílá výplň by tam přepsala obsah, který sousední
snímek má v pořádku. Vynechané místo se vyplní z něj; jen když ho nemá žádný
snímek, zůstane bílé.

Když překryv chybí úplně, rozhoduje obsah u okrajů. Posunul-li prohlížeč
přesně o celou obrazovku, text sahá až ke spodnímu okraji prvního snímku
a hned pokračuje u horního okraje druhého – snímky se pak spojí na doraz.
Když jsou naopak oba okraje prázdné, skončila stránka a začíná nová.
"""

from __future__ import annotations

import os
from array import array
from dataclasses import dataclass, field
from typing import Callable, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageStat

SCORE_STRIDE = 4           # každý čtvrtý řádek stačí na seřazení kandidátů
MIN_OVERLAP_PX = 16        # Page Down často nechá překryv jen pár řádků
MIN_TEXT_ROWS = 12         # kolik řádků textu musí překryv obsahovat
EDGE_BAND_RATIO = 0.04     # jak vysoký okraj snímku se zkoumá u nulového překryvu
EDGE_TEXT_ROWS = 3         # od kolika řádků textu považujeme okraj za zaplněný
USEFUL_SHIFT_RATIO = 0.10  # menší posun než desetina výšky je pro skládání bezcenný
TEXT_ROW_DELTA = 15        # o kolik musí být řádek tmavší než pozadí
OUTLIER_MAD_FACTOR = 4.0   # odchylka od obvyklého posuvu, kterou ještě tolerujeme
MIN_TOLERANCE_PX = 24      # minimální tolerance, když je posuv naprosto pravidelný
# Naměřeno na skutečných snímcích: pravý překryv dává 0.000-0.031 (horní konec
# u snímků, které klient RDP překreslil s jinými detaily), zatímco nejlepší
# možná shoda dvou různých stránek 0.060-0.124. Práh leží uprostřed té mezery.
# Proti falešné shodě navíc chrání kontrola odlehlých posunů v plan_ribbon().
MAX_VERIFY_DIFF = 0.045    # max. průměrný rozdíl pixelů v překryvu (0.0-1.0)
CANDIDATES = 8             # kolik nejlepších posunů ověřit na pixelech
PROFILE_MARGIN = 0.04      # okraje vynecháme (posuvník, rámeček okna)
A4_RATIO = 297.0 / 210.0
CUT_SEARCH_RATIO = 0.18    # jak daleko od cílové výšky hledat bílé místo
CUT_BAND = 6               # výška pásu, který se hodnotí jako místo řezu


class StitchError(RuntimeError):
    """Skládání se nepovedlo. Nikdy nesmí zahodit původní snímky."""


@dataclass
class Placement:
    """Umístění jednoho snímku v pásu."""

    path: str
    top: int
    width: int
    height: int
    shift: int | None = None      # o kolik se obsah posunul oproti předchozímu
    match_error: float = 0.0
    verified: bool = False
    page_break: bool = False   # tímto snímkem začíná nová stránka

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass
class RibbonPlan:
    placements: list[Placement]
    width: int
    height: int
    profile: list[float] = field(default_factory=list)

    @property
    def unverified(self) -> list[int]:
        return [i for i, p in enumerate(self.placements) if i and not p.verified]


# ---------------------------------------------------------------------------
# Profil řádků
# ---------------------------------------------------------------------------
def row_profile(image: Image.Image, margin_ratio: float = PROFILE_MARGIN) -> list[float]:
    """Průměrný jas každého řádku. Okraje se vynechají kvůli posuvníku."""
    width, height = image.size
    margin = int(width * margin_ratio)
    if width - 2 * margin < 8:
        margin = 0
    band = image.convert("L")
    if margin:
        band = band.crop((margin, 0, width - margin, height))
    column = band.convert("F").resize((1, height), Image.Resampling.BOX)
    values = array("f")
    values.frombytes(column.tobytes())
    return list(values)


def _text_rows(profile: Sequence[float]) -> list[int]:
    """Kumulativní počet řádků, které nesou text (jsou tmavší než pozadí)."""
    if not profile:
        return [0]
    ordered = sorted(profile)
    background = ordered[int(len(ordered) * 0.9)]
    limit = background - TEXT_ROW_DELTA
    counts = [0]
    total = 0
    for value in profile:
        if value < limit:
            total += 1
        counts.append(total)
    return counts


def _has_content(counts: Sequence[int], start: int, count: int) -> bool:
    """Je v úseku dost textu, aby se podle něj dalo zarovnávat?"""
    end = min(start + count, len(counts) - 1)
    if end <= start:
        return False
    return counts[end] - counts[start] >= MIN_TEXT_ROWS


def _candidate_shifts(
    prev: Sequence[float], curr: Sequence[float], min_overlap: int
) -> list[tuple[float, int]]:
    """Posuny seřazené podle shody profilů, od nejlepšího (nejnižší rozdíl).

    Nepoužívá se korelační koeficient: `Page Down` nechá překryv často jen
    několik desítek řádků a normalizovaná korelace je na tak malém vzorku
    nestabilní. Přímý rozdíl profilů je absolutní míra, která na délce
    překryvu nezávisí.

    Úsek bez textu se zahazuje, a to na **obou** snímcích – bílý pruh sedí
    na jakýkoli jiný bílý pruh a vyrobil by přesvědčivou, ale nesmyslnou shodu.
    """
    height = len(prev)
    max_shift = height - min_overlap
    if max_shift <= 0:
        return []

    stride = SCORE_STRIDE
    prev_text = _text_rows(prev)
    curr_text = _text_rows(curr)

    scored: list[tuple[float, int]] = []
    for shift in range(0, max_shift + 1):
        overlap = height - shift
        count = (overlap + stride - 1) // stride
        if count < 4:
            continue
        if not _has_content(curr_text, 0, overlap):
            continue
        if not _has_content(prev_text, shift, overlap):
            continue
        total = 0.0
        for i in range(count):
            diff = prev[shift + i * stride] - curr[i * stride]
            total += diff if diff >= 0.0 else -diff
        scored.append((total / count, shift))
    scored.sort()
    return scored


def _overlap_difference(
    prev: Image.Image, curr: Image.Image, shift: int
) -> float:
    """Průměrný rozdíl pixelů v překryvu, v plném rozlišení (0.0-1.0)."""
    height = prev.size[1]
    overlap = height - shift
    if overlap <= 0:
        return 1.0
    top = prev.crop((0, shift, prev.size[0], height)).convert("L")
    bottom = curr.crop((0, 0, curr.size[0], overlap)).convert("L")
    if top.size != bottom.size:
        return 1.0
    return ImageStat.Stat(ImageChops.difference(top, bottom)).mean[0] / 255.0


def find_shift(
    prev_image: Image.Image,
    curr_image: Image.Image,
    prev_profile: Sequence[float] | None = None,
    curr_profile: Sequence[float] | None = None,
    prefer: int | None = None,
    window: int = 0,
) -> tuple[int | None, float]:
    """Vrátí (posun v pixelech, chyba shody). Posun `None` = nelze určit.

    `prefer` omezí hledání na okolí očekávaného posunu. Slouží k přezkoumání
    odlehlých výsledků: dokumenty mívají na každé stránce stejné záhlaví,
    takže překryv může přesvědčivě sednout i na nesprávné místo.

    Profil řádků slouží **jen k seřazení** kandidátů; o výsledku rozhoduje
    porovnání skutečných pixelů v překryvu. Na hodnotu profilu se proto
    nesmí nasadit strop – naměřeno na skutečném spoji: správný posun byl
    v žebříčku první, ale jeho skóre profilu 8.6 by ho vyřadilo dřív, než
    by se vůbec dostal na kontrolu pixelů. Práci místo toho omezuje počet
    ověřených kandidátů. Řádky textu se opakují
    pravidelně, takže profil sedí i při posunu o celý řádek – teprve pixely
    odliší tentýž text od jiného textu na stejné pozici.
    """
    if prev_image.size != curr_image.size:
        return None, 0.0
    height = prev_image.size[1]
    min_overlap = min(max(MIN_OVERLAP_PX, height // 50), height // 2)
    if height <= min_overlap:
        return None, 0.0

    prev_profile = list(prev_profile if prev_profile is not None else row_profile(prev_image))
    curr_profile = list(curr_profile if curr_profile is not None else row_profile(curr_image))

    checked = 0
    for score, shift in _candidate_shifts(prev_profile, curr_profile, min_overlap):
        if prefer is not None and abs(shift - prefer) > window:
            continue
        if _overlap_difference(prev_image, curr_image, shift) <= MAX_VERIFY_DIFF:
            return shift, score
        checked += 1
        if checked >= CANDIDATES:
            break
    return None, 0.0


# ---------------------------------------------------------------------------
# Plán pásu
# ---------------------------------------------------------------------------
def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return 0.0
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _usual_shift(shifts: Sequence[int | None], height: int) -> tuple[int, int]:
    """Obvyklý posun a tolerance odvozená z jeho rozptylu.

    Prohlížeč posouvá `Page Down` konstantně, takže rozptyl bývá malý a
    tolerance přísná. U nepravidelného posuvu se sama uvolní.
    """
    known = [s for s in shifts if s is not None]
    if not known:
        return height, MIN_TOLERANCE_PX
    usual = int(round(_median(known)))
    deviation = _median([abs(s - usual) for s in known])
    tolerance = max(MIN_TOLERANCE_PX, int(round(OUTLIER_MAD_FACTOR * deviation)))
    return usual, tolerance


def _recheck_outliers(
    paths: Sequence[str],
    profiles: Sequence[Sequence[float]],
    shifts: list[int | None],
    errors: list[float],
    usual: int,
    tolerance: int,
    log: Callable[[str], None] | None = None,
) -> None:
    """Přezkoumá spoje, které se vymykají obvyklému posunu, v jeho okolí."""
    for index in range(1, len(shifts)):
        shift = shifts[index]
        if shift is not None and abs(shift - usual) <= tolerance:
            continue
        with Image.open(paths[index - 1]) as prev, Image.open(paths[index]) as curr:
            prev.load()
            curr.load()
            better, error = find_shift(
                prev, curr, profiles[index - 1], profiles[index],
                prefer=usual, window=tolerance,
            )
        if better is None:
            if shift is not None:
                # Podezřelý posun raději zahodíme než abychom mu věřili –
                # nastoupí obvyklý posun, který je u odlehlé hodnoty spolehlivější.
                if log:
                    log(
                        f"Skládání: posun {shift} px u snímku {index + 1} se vymyká "
                        f"obvyklým {usual} px a v jejich okolí nemá oporu, zahozen"
                    )
                shifts[index] = None
                errors[index] = 0.0
            continue
        if log and shift is not None and better != shift:
            log(
                f"Skládání: posun {shift} px u snímku {index + 1} se vymykal "
                f"obvyklým {usual} px, po přezkoumání {better} px"
            )
        shifts[index] = better
        errors[index] = error


def _edge_has_text(profile: Sequence[float], from_top: bool) -> bool:
    """Sahá text až k okraji snímku?"""
    height = len(profile)
    band = max(8, int(height * EDGE_BAND_RATIO))
    counts = _text_rows(profile)
    start = 0 if from_top else max(0, height - band)
    return _has_content_at_least(counts, start, band, EDGE_TEXT_ROWS)


def _has_content_at_least(
    counts: Sequence[int], start: int, count: int, minimum: int
) -> bool:
    end = min(start + count, len(counts) - 1)
    if end <= start:
        return False
    return counts[end] - counts[start] >= minimum


def _resolve_gap(
    prev_path: str,
    curr_path: str,
    prev_profile: Sequence[float],
    curr_profile: Sequence[float],
    usual: int,
    height: int,
    index: int,
    log: Callable[[str], None] | None = None,
) -> tuple[int, bool, bool]:
    """Rozhodne, co s dvojicí, u které se nenašel překryv.

    Vrací (posun, ověřeno, zlom_stránky). Postup:

    1. Sedí-li pixely při obvyklém posunu, šlo jen o prázdný překryv –
       použije se obvyklý posun.
    2. Sahá-li text ke spodnímu okraji prvního snímku nebo k hornímu okraji
       druhého, prohlížeč posunul přesně o obrazovku a snímky se spojí na doraz.
    3. Jinak stránka skončila a začíná nová.
    """
    if usual < height:
        with Image.open(prev_path) as prev, Image.open(curr_path) as curr:
            prev.load()
            curr.load()
            difference = _overlap_difference(prev, curr, usual)
        if difference <= MAX_VERIFY_DIFF:
            if log:
                log(
                    f"Skládání: u snímku {index + 1} nelze překryv ověřit "
                    f"(prázdné místo), použit obvyklý posun {usual} px"
                )
            return usual, False, False

    bottom_filled = _edge_has_text(prev_profile, from_top=False)
    top_filled = _edge_has_text(curr_profile, from_top=True)
    if bottom_filled or top_filled:
        if log:
            log(
                f"Skládání: snímek {index + 1} navazuje bez překryvu "
                "(posun o celou obrazovku), spojeno na doraz"
            )
        return height, False, False

    if log:
        log(
            f"Skládání: snímek {index + 1} začíná novou stránku "
            "(prázdný spodní i horní okraj)"
        )
    return height, False, True


def plan_ribbon(
    image_paths: Sequence[str],
    log: Callable[[str], None] | None = None,
) -> RibbonPlan:
    """Spočítá, kam v souvislém pásu patří jednotlivé snímky.

    Nejdřív se určí posuny mezi sousedními snímky, teprve pak se skládá pás.
    Když se posun najít nedá (v překryvu je jen prázdné místo, například
    mezera mezi stránkami), použije se medián ostatních posunů – prohlížeč
    posouvá `Page Down` konstantně, takže je to výrazně lepší odhad než
    navázání bez překryvu.
    """
    paths = [p for p in image_paths if p]
    if not paths:
        raise StitchError("Nejsou k dispozici žádné snímky ke složení.")

    profiles: list[list[float]] = []
    shifts: list[int | None] = [None]
    errors: list[float] = [0.0]
    width = height = 0
    previous: Image.Image | None = None

    try:
        for index, path in enumerate(paths):
            image = Image.open(path)
            image.load()
            if index == 0:
                width, height = image.size
            elif image.size != (width, height):
                raise StitchError(
                    f"Snímek {os.path.basename(path)} má jiný rozměr "
                    f"({image.size[0]}x{image.size[1]} místo {width}x{height})."
                )
            profile = row_profile(image)
            if previous is not None:
                shift, error = find_shift(previous, image, profiles[-1], profile)
                shifts.append(shift)
                errors.append(error)
                previous.close()
            profiles.append(profile)
            previous = image
    finally:
        if previous is not None:
            previous.close()

    fallback, tolerance = _usual_shift(shifts[1:], height)
    if fallback:
        _recheck_outliers(paths, profiles, shifts, errors, fallback, tolerance, log)

    placements: list[Placement] = []
    estimated = 0
    breaks = 0
    top = 0
    for index, path in enumerate(paths):
        step = shifts[index]
        verified = True
        page_break = False
        if index:
            if step is None:
                # Nenašel se překryv. Než ho odhadneme, ověříme, jestli snímky
                # vůbec navazují – jinak jde o zlom stránky a odhad by slepil
                # dvě různé stránky na náhodné pozici.
                step, verified, page_break = _resolve_gap(
                    paths[index - 1],
                    paths[index],
                    profiles[index - 1],
                    profiles[index],
                    fallback,
                    height,
                    index,
                    log,
                )
                if page_break:
                    breaks += 1
                else:
                    estimated += 1
            top += step
        placements.append(
            Placement(
                path=path,
                top=top,
                width=width,
                height=height,
                shift=step if index else None,
                match_error=errors[index],
                verified=verified if index else True,
                page_break=page_break,
            )
        )

    ribbon_height = placements[-1].bottom
    profile_sum = [0.0] * ribbon_height
    profile_count = [0] * ribbon_height
    for placement, profile in zip(placements, profiles):
        for i, value in enumerate(profile):
            profile_sum[placement.top + i] += value
            profile_count[placement.top + i] += 1
    ribbon_profile = [
        (profile_sum[i] / profile_count[i]) if profile_count[i] else 255.0
        for i in range(ribbon_height)
    ]

    known = sorted(s for s in shifts[1:] if s is not None)
    # Ověřený spoj s nepatrným posunem znamená, že se obraz skoro nehnul –
    # pro skládání je bezcenný. Počítáme jen spoje se skutečným posunem.
    useful = sum(
        1
        for p in placements[1:]
        if p.verified and p.shift and p.shift > height * USEFUL_SHIFT_RATIO
    )
    joins = len(placements) - 1
    if log and joins >= 3 and useful * 3 < joins:
        log(
            f"Skládání: POZOR – použitelný překryv má jen {useful} z {joins} spojů. "
            "Sousední snímky na sebe nenavazují překryvem, takže stránky nelze "
            "poskládat spolehlivě. Posun na jeden Page Down je větší než výška "
            "snímané oblasti – zvětšete snímanou oblast na celou plochu dokumentu, "
            "posouvejte dokument po menších krocích, nebo skládání vypněte."
        )
    if log and known:
        log(
            f"Skládání: {len(paths)} snímků, posun {known[0]}–{known[-1]} px "
            f"(obvykle {fallback}), pás vysoký {ribbon_height} px"
            + (f", odhadnutých spojů: {estimated}" if estimated else "")
            + (f", zlomů stránky: {breaks}" if breaks else "")
        )
    return RibbonPlan(
        placements=placements, width=width, height=ribbon_height, profile=ribbon_profile
    )


# ---------------------------------------------------------------------------
# Řezání na stránky
# ---------------------------------------------------------------------------
def find_cuts(plan: RibbonPlan, page_height: int) -> list[tuple[int, int]]:
    """Rozdělí pás na stránky.

    Přednost mají **zlomy stránky** – místa, kde snímky prokazatelně
    nenavazovaly. Jen úsek výrazně delší než cílová výška se ještě dělí,
    a to co nejsvětlejším místem: má-li prohlížeč mezi stránkami mezeru,
    řez si ji sám najde.
    """
    page_height = max(32, int(page_height))
    boundaries = [0]
    for placement in plan.placements[1:]:
        if placement.page_break and placement.top > boundaries[-1]:
            boundaries.append(placement.top)
    boundaries.append(plan.height)

    cuts: list[tuple[int, int]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start <= page_height * 1.5:
            cuts.append((start, end))
        else:
            cuts.extend(_split_segment(plan, start, end, page_height))
    return [(a, b) for a, b in cuts if b > a]


def _split_segment(
    plan: RibbonPlan, start: int, end: int, page_height: int
) -> list[tuple[int, int]]:
    """Rozdělí příliš dlouhý úsek pásu v nejsvětlejších místech."""
    profile = plan.profile
    search = max(8, int(page_height * CUT_SEARCH_RATIO))
    cuts: list[tuple[int, int]] = []
    position = start
    while position < end:
        if end - position <= page_height * 1.2:
            cuts.append((position, end))
            break
        target = position + page_height
        low = max(position + page_height // 2, target - search)
        high = min(end - CUT_BAND, target + search)
        best_row, best_ink = target, None
        for row in range(low, high + 1):
            band = profile[row : row + CUT_BAND]
            if not band:
                continue
            ink = sum(255.0 - value for value in band)
            if best_ink is None or ink < best_ink:
                best_ink, best_row = ink, row
        cut = best_row + CUT_BAND // 2
        cuts.append((position, cut))
        position = cut
    return cuts


def _paste_stencil(size: tuple[int, int], holes: Sequence[tuple[int, int, int, int]],
                   offset_y: int):
    """Maska pro vkládání: 0 tam, kde se kreslit nemá."""
    if not holes:
        return None
    stencil = Image.new("L", size, 255)
    draw = ImageDraw.Draw(stencil)
    width, height = size
    for left, top, right, bottom in holes:
        y0 = max(0, top - offset_y)
        y1 = min(height, bottom - offset_y)
        x0 = max(0, left)
        x1 = min(width, right)
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=0)
    return stencil


def render_pages(
    plan: RibbonPlan,
    cuts: Sequence[tuple[int, int]],
    out_dir: str,
    prefix: str = "page_",
    digits: int = 4,
    log: Callable[[str], None] | None = None,
    sources: Sequence[str] | None = None,
    holes: Sequence[object] | None = None,
) -> list[str]:
    """Vykreslí stránky pásu do souborů. Otevírá jen snímky, které stránka potřebuje.

    `sources` umožní vykreslit z jiných souborů, než podle kterých se zarovnávalo.
    `holes` jsou obdélníky v souřadnicích snímku, které se nemají kreslit vůbec –
    vyplní je sousední snímek, který na tom místě obsah má.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    by_path = dict(zip((p.path for p in plan.placements), sources or ()))
    hole_boxes = [
        (r.x, r.y, r.x + r.width, r.y + r.height)
        for r in (holes or [])
        if r.width > 0 and r.height > 0
    ]

    for number, (top, bottom) in enumerate(cuts, start=1):
        height = bottom - top
        if height <= 0:
            continue
        canvas = Image.new("RGB", (plan.width, height), "white")
        for placement in plan.placements:
            if placement.bottom <= top or placement.top >= bottom:
                continue
            with Image.open(by_path.get(placement.path, placement.path)) as source:
                source = source.convert("RGB")
                src_top = max(0, top - placement.top)
                src_bottom = min(placement.height, bottom - placement.top)
                piece = source.crop((0, src_top, placement.width, src_bottom))
            stencil = _paste_stencil(piece.size, hole_boxes, src_top)
            canvas.paste(piece, (0, placement.top + src_top - top), stencil)

        path = os.path.join(out_dir, f"{prefix}{number:0{digits}d}.png")
        canvas.save(path, "PNG")
        canvas.close()
        written.append(path)
        if log:
            log(f"Skládání: stránka {number}/{len(cuts)} ({plan.width}x{height} px)")
    return written


# ---------------------------------------------------------------------------
def stitch_pages(
    image_paths: Sequence[str],
    out_dir: str,
    page_height: int = 0,
    log: Callable[[str], None] | None = None,
    render_paths: Sequence[str] | None = None,
    holes: Sequence[object] | None = None,
) -> list[str]:
    """Složí snímky do pásu a rozřeže je na stránky. Vrací cesty k novým PNG.

    `page_height` 0 znamená poměr A4 podle šířky pásu.
    """
    plan = plan_ribbon(image_paths, log=log)
    if page_height <= 0:
        page_height = int(round(plan.width * A4_RATIO))
    cuts = find_cuts(plan, page_height)
    if log:
        unverified = plan.unverified
        log(
            f"Skládání: {len(plan.placements)} snímků -> {len(cuts)} stránek "
            f"(výška stránky {page_height} px)"
            + (f", neověřených spojů: {len(unverified)}" if unverified else "")
        )
    return render_pages(
        plan, cuts, out_dir, log=log, sources=render_paths, holes=holes
    )
