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

Když se posun nepodaří určit, snímek se připojí bez překryvu a událost se
zapíše do logu – dokument tak zůstane celý, jen bez záruky na spoji.
"""

from __future__ import annotations

import os
from array import array
from dataclasses import dataclass, field
from typing import Callable, Sequence

from PIL import Image, ImageChops, ImageStat

SCORE_STRIDE = 4           # každý čtvrtý řádek stačí na seřazení kandidátů
MIN_OVERLAP_PX = 16        # Page Down často nechá překryv jen pár řádků
MIN_PROFILE_STD = 2.0      # v překryvu musí být text, ne prázdný okraj
OUTLIER_MAD_FACTOR = 4.0   # odchylka od obvyklého posuvu, kterou ještě tolerujeme
MIN_TOLERANCE_PX = 24      # minimální tolerance, když je posuv naprosto pravidelný
MAX_PROFILE_DIFF = 6.0     # max. průměrný rozdíl profilů (odstíny šedi)
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


def _downsample(profile: Sequence[float], factor: int) -> list[float]:
    return [
        sum(profile[i : i + factor]) / len(profile[i : i + factor])
        for i in range(0, len(profile) - factor + 1, factor)
    ]


def _segment_std(profile: Sequence[float], start: int, count: int) -> float:
    """Rozptyl profilu v úseku – prázdný bílý okraj má nulový."""
    if count < 2:
        return 0.0
    total = 0.0
    for i in range(count):
        total += profile[start + i]
    mean = total / count
    var = 0.0
    for i in range(count):
        delta = profile[start + i] - mean
        var += delta * delta
    return (var / count) ** 0.5


def _mean_abs_diff(
    a: Sequence[float], b: Sequence[float], a_from: int, count: int
) -> float:
    """Průměrný absolutní rozdíl a[a_from:a_from+count] proti b[:count]."""
    total = 0.0
    for i in range(count):
        diff = a[a_from + i] - b[i]
        total += diff if diff >= 0.0 else -diff
    return total / count


def _sample_stats(
    profile: Sequence[float], stride: int
) -> tuple[list[float], list[float]]:
    """Kumulativní součty vzorkovaného profilu – umožní počítat rozptyl v O(1)."""
    sums = [0.0]
    squares = [0.0]
    for i in range(0, len(profile), stride):
        value = profile[i]
        sums.append(sums[-1] + value)
        squares.append(squares[-1] + value * value)
    return sums, squares


def _std_from_prefix(
    sums: Sequence[float], squares: Sequence[float], count: int
) -> float:
    if count < 2:
        return 0.0
    total = sums[count]
    var = squares[count] / count - (total / count) ** 2
    return var ** 0.5 if var > 0.0 else 0.0


def _candidate_shifts(
    prev: Sequence[float], curr: Sequence[float], min_overlap: int
) -> list[tuple[float, int]]:
    """Posuny seřazené podle shody profilů, od nejlepšího (nejnižší rozdíl).

    Nepoužívá se korelační koeficient: `Page Down` nechá překryv často jen
    několik desítek řádků a normalizovaná korelace je na tak malém vzorku
    nestabilní. Přímý rozdíl profilů je absolutní míra, která na délce
    překryvu nezávisí. Úseky bez textu se zahazují – v prázdném bílém okraji
    sedí na sebe cokoli.
    """
    height = len(prev)
    max_shift = height - min_overlap
    if max_shift <= 0:
        return []

    stride = SCORE_STRIDE
    sums, squares = _sample_stats(curr, stride)

    scored: list[tuple[float, int]] = []
    for shift in range(0, max_shift + 1):
        count = (height - shift + stride - 1) // stride
        if count < 4:
            continue
        if _std_from_prefix(sums, squares, count) < MIN_PROFILE_STD:
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

    Profil řádků slouží jen jako rychlé předsíto; o výsledku rozhoduje
    porovnání skutečných pixelů v překryvu. Řádky textu se opakují
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

    for score, shift in _candidate_shifts(prev_profile, curr_profile, min_overlap):
        if score > MAX_PROFILE_DIFF:
            break
        if prefer is not None and abs(shift - prefer) > window:
            continue
        if _overlap_difference(prev_image, curr_image, shift) <= MAX_VERIFY_DIFF:
            return shift, score
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
    top = 0
    for index, path in enumerate(paths):
        step = shifts[index]
        verified = True
        if index:
            if step is None:
                step = fallback
                verified = False
                estimated += 1
                if log:
                    log(
                        f"Skládání: u snímku {index + 1} nelze překryv ověřit "
                        f"(prázdné místo), použit obvyklý posun {fallback} px"
                    )
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
    if log and known:
        log(
            f"Skládání: {len(paths)} snímků, posun {known[0]}–{known[-1]} px "
            f"(obvykle {fallback}), pás vysoký {ribbon_height} px"
            + (f", odhadnutých spojů: {estimated}" if estimated else "")
        )
    return RibbonPlan(
        placements=placements, width=width, height=ribbon_height, profile=ribbon_profile
    )


# ---------------------------------------------------------------------------
# Řezání na stránky
# ---------------------------------------------------------------------------
def find_cuts(plan: RibbonPlan, page_height: int) -> list[tuple[int, int]]:
    """Rozdělí pás na stránky, řezy vede co nejsvětlejším místem.

    Když prohlížeč mezi stránkami dokumentu nechává mezeru, je tam nejméně
    inkoustu a řez si ji sám najde. Jinak se řeže mezi řádky textu.
    """
    page_height = max(32, int(page_height))
    if plan.height <= page_height:
        return [(0, plan.height)]

    profile = plan.profile
    search = max(8, int(page_height * CUT_SEARCH_RATIO))
    cuts: list[tuple[int, int]] = []
    start = 0

    while start < plan.height:
        if plan.height - start <= page_height * 1.2:
            cuts.append((start, plan.height))
            break
        target = start + page_height
        low = max(start + page_height // 2, target - search)
        high = min(plan.height - CUT_BAND, target + search)
        best_row, best_ink = target, None
        for row in range(low, high + 1):
            band = profile[row : row + CUT_BAND]
            if not band:
                continue
            ink = sum(255.0 - value for value in band)
            if best_ink is None or ink < best_ink:
                best_ink, best_row = ink, row
        cut = best_row + CUT_BAND // 2
        cuts.append((start, cut))
        start = cut
    return cuts


def render_pages(
    plan: RibbonPlan,
    cuts: Sequence[tuple[int, int]],
    out_dir: str,
    prefix: str = "page_",
    digits: int = 4,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Vykreslí stránky pásu do souborů. Otevírá jen snímky, které stránka potřebuje."""
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    for number, (top, bottom) in enumerate(cuts, start=1):
        height = bottom - top
        if height <= 0:
            continue
        canvas = Image.new("RGB", (plan.width, height), "white")
        for placement in plan.placements:
            if placement.bottom <= top or placement.top >= bottom:
                continue
            with Image.open(placement.path) as source:
                source = source.convert("RGB")
                src_top = max(0, top - placement.top)
                src_bottom = min(placement.height, bottom - placement.top)
                piece = source.crop((0, src_top, placement.width, src_bottom))
            canvas.paste(piece, (0, placement.top + src_top - top))

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
    return render_pages(plan, cuts, out_dir, log=log)
