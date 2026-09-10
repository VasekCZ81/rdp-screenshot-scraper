# RDP Screenshot Scraper

Windows aplikace, která automaticky snímá obsah okna **již spuštěné a přihlášené**
RDP relace, posouvá dokument klávesou `Page Down`, sama rozpozná konec dokumentu
a ze snímků sestaví jedno PDF s vyhledatelným textem.

## Snímá se okno, ne obrazovka

Snímek nevzniká z plochy monitoru, ale přímo z okna `mstsc.exe` přes
`PrintWindow(PW_RENDERFULLCONTENT)` – viz [src/capture.py](src/capture.py).
Z toho plynou dvě věci, na kterých stojí celá aplikace:

1. **Okno RDP smí být větší než monitor.** Windows vykreslí i tu část okna, která
   leží mimo obrazovku. Relace může být vysoká 3600 px na monitoru vysokém
   2160 px – celá stránka PDF se pak vejde do jednoho snímku ve výrazně vyšším
   rozlišení a nemusí se posouvat ani skládat. Jak takovou relaci založit, popisuje
   [Rozlišení – jak dostat ostrý obraz](#rozlišení--jak-dostat-ostrý-obraz).
2. **Na okno RDP smí cokoli ležet.** Total Commander je klidně může celé překrývat,
   snímek to nijak neovlivní.

Souřadnice snímané oblasti jsou proto pixely **client rectu okna RDP**, ne
souřadnice plochy. Nezávisí tak na tom, kde okno leží, a vydrží i po restartu
aplikace – ukládají se do `config.json`.

Desktopové snímání (`mss`, `BitBlt` nad plochou) se nepoužívá ani jako tichý
fallback: vracelo by obsah překrývajícího okna nebo černou plochu.

## Klíčová podmínka

> Screenshot vzniká **výhradně tehdy**, když `GetForegroundWindow()` potvrdí jako
> aktivní okno **Total Commanderu**. Při aktivním okně RDP se screenshot nikdy nepořizuje.

Tato podmínka je vynucena v jediné metodě `AutomationController._capture_with_total_commander()`
v [src/automation.py](src/automation.py) – nikde jinde v kódu snímek nevzniká. Postup je:

1. aktivace okna Total Commanderu,
2. čekání na dokončení přepnutí,
3. ověření `GetForegroundWindow()`,
4. druhé ověření po krátké prodlevě,
5. teprve poté screenshot.

Když se aktivace nezdaří (výchozí 3 pokusy po 400 ms), **snímek nevznikne** a
automatizace se zastaví s chybou. Stejně tak se `Page Down` odešle jen tehdy,
když je ověřeným aktivním oknem RDP relace.

---

## Vše potřebné je součástí projektu – nic se nestahuje

Adresář [vendor/](vendor/) obsahuje všechna kolečka (`.whl`), která projekt
potřebuje. Instalace i sestavení `.exe` proto proběhnou **bez připojení
k internetu** – na cílovém počítači stačí mít nainstalovaný Python.

Podrobnosti (které verze Pythonu jsou pokryté, jak doplnit další prostředí)
najdete v [vendor/README.md](vendor/README.md).

## Instalace pro vývoj

```bat
python -m venv .venv
.venv\Scripts\activate
pip install --no-index --find-links vendor -r requirements.txt
python src\main.py
```

Nebo jedním příkazem, který si prostředí připraví sám:

```bat
run.bat
```

Aplikace nepotřebuje `pywin32` – práce s Win32 API včetně snímání okna je
řešena přes `ctypes` ze standardní knihovny. Za běhu je potřeba jediná
knihovna, `Pillow` (obrázky). GUI staví na `tkinter`, který je součástí
instalace Pythonu.

## Build

```bat
build.bat
```

Skript vytvoří `.venv`, nainstaluje závislosti **offline z `vendor\`**, spustí
testy a sestaví `dist\RdpScreenshotScraper.exe` (jeden soubor, bez okna konzole).

Uživatel na cílovém PC nemusí instalovat Python, pip ani žádné knihovny.
Nejsou potřeba ani žádné dodatečné DLL – `.exe` obsahuje vše potřebné včetně
runtime Pythonu a Tcl/Tk.

Ruční varianta bez `build.bat`:

```bat
pip install --no-index --find-links vendor -r requirements.txt
pyinstaller --noconfirm --clean RdpScreenshotScraper.spec
```

> Pokud build hlásí, že nelze přepsat `dist\RdpScreenshotScraper.exe`,
> běží ještě starší instance aplikace – nejdřív ji zavřete.

## Testy

```bat
set PYTHONPATH=%CD%\src;%CD%\tests
.venv\Scripts\python.exe -m unittest discover -s tests -t tests -v
```

Testy simulují Win32 vrstvu, takže běží bez Total Commanderu i bez RDP relace.

---

## Použití

> ### Podmínka: nejdřív vyplň adresu RDP relace
>
> Adresa serveru **není předvyplněná**. Bez ní aplikace nepozná, které okno
> *Připojení ke vzdálené ploše* je to správné, a snímání **nespustí** – místo
> toho vypíše chybu a otevře Nastavení.
>
> Po prvním spuštění tedy otevři **Nastavení** a do pole
> **„Adresa RDP relace (povinné)“** zadej adresu serveru, ke kterému jsi
> připojen – například `192.168.1.100` nebo `server.firma.local`. Stačí část,
> která se objevuje v titulku okna RDP. Hodnota se uloží do `config.json`
> vedle `.exe`, takže se zadává jen jednou.

1. Spusť **Total Commander** a **RDP Screenshot Scraper**.
2. V **Nastavení** vyplň **adresu RDP relace** a případně rozlišení relace
   (výchozí 2560×3600).
3. Klikni na **Založit RDP relaci…** a přihlas se. Aplikace relaci založí
   v rozlišení, které určuje výslednou kvalitu – viz
   [Rozlišení – jak dostat ostrý obraz](#rozlišení--jak-dostat-ostrý-obraz).
   *(Máš-li relaci už otevřenou v potřebném rozlišení, stačí
   **Obnovit seznam oken**.)*
4. Otevři v RDP dokument na první pozici.
5. Klikni **Vybrat oblast**. Aplikace nejdřív sama roztáhne okno RDP na celou
   plochu relace (viz níže), pak pořídí jeho snímek a zobrazí ho zmenšený.
6. Tažením myši označ v tom snímku oblast dokumentu (`ESC` výběr zruší).
   Označit lze i tu část relace, která je mimo obrazovku. Volba se uloží do
   `config.json`, takže příště už se zadávat nemusí.
7. Klikni **Spustit**.
8. Aplikace postupně pořídí screenshoty.
9. Po dosažení konce dokumentu vytvoří výsledné PDF s vyhledatelným textem.

> **Upozornění:** Během automatického snímání uživatel nemá ručně měnit obsah
> RDP dokumentu ani zavírat Total Commander nebo RDP relaci.

### Roztažení okna RDP

Aby se do snímku vešla celá relace, musí být okno `mstsc.exe` tak velké, aby ji
pojalo celou – tedy typicky **vyšší než monitor**. Myší to udělat nejde: spodní
okraj okna se pod dolní hranu obrazovky táhnout nedá.

Slouží k tomu tlačítko **Roztáhnout okno RDP**, které:

1. obnoví okno, pokud je minimalizované nebo maximalizované
   (maximalizované okno Windows nezvětší, `SetWindowPos` na něm nic neudělá),
2. přesune ho do levého horního rohu monitoru a zvětší na 4096×4096,
3. `mstsc` si velikost sám doladí přesně na rozměr relace a zmizí posuvníky,
4. když je relace menší než okno, `mstsc` ji vycentruje do černých pruhů –
   ty aplikace najde a okno na ten rozměr stáhne.

Původní umístění si aplikace zapamatuje, takže se stejným tlačítkem
(**Vrátit okno RDP**) vrátí okno přesně tam, kde bylo.

Roztažení proběhne **automaticky** před výběrem oblasti i před spuštěním
snímání, takže na tlačítko není nutné sahat – hodí se, když si chcete relaci
jen prohlédnout nebo v ní něco nastavit.

> Okno pak přesahuje mimo obrazovku a je vidět jen jeho horní část. Tak to má
> být: snímá se obsah okna, ne to, co je na monitoru.

### Rozmístění oken

Okno Total Commanderu **smí okno RDP překrývat** – snímá se obsah okna, ne to, co
je zrovna vidět na ploše. Obě okna tedy mohou být kdekoli, klidně přes sebe, a
okno RDP může přesahovat mimo obrazovku.

Jediné, co platit musí:

* okno RDP **nesmí být minimalizované** – minimalizované okno Windows nevykreslují
  a `PrintWindow` by vrátil prázdnou plochu. Aplikace to kontroluje před startem
  i před každým snímkem a v takovém případě snímek neuloží.

Pokud během běhu omylem aktivujete jiné okno, aplikace v dalším kroku správné
okno znovu aktivuje a znovu ověří – snímek s nesprávným aktivním oknem nevznikne.

### Nouzové zastavení

* globální zkratka **`Ctrl + Shift + F12`** (registrovaná přes `RegisterHotKey`,
  do RDP relace se proto neodešle jako běžný vstup),
* klávesa **`ESC`**, pokud je aktivní okno aplikace,
* tlačítko **Ukončit snímání** v GUI.

Pokud se globální zkratku nepodaří zaregistrovat (obsadila ji jiná aplikace),
aplikace to napíše do průběhu a zbývají zbylé dvě možnosti.

---

## Výstupy

Vše se ukládá do pracovního adresáře, tj. adresáře, ve kterém leží `.exe`
(při běhu ze zdrojáků do kořene projektu). Každý běh má vlastní podadresář, takže
se starší snímky nikdy nepřepíšou:

```
RdpScreenshotScraper.exe
config.json
captures/
    2026-09-08_184500/
        page_0001.png
        page_0002.png
        page_0003.png
        duplicates/
            dup_0001.png          <- potvrzení konce, do PDF se nedostane
            dup_0002.png
        masked/                   <- jen když je vybraná oblast k vymazání
            page_0001.png
            page_0002.png
        scraper.log
        RDP_capture_2026-09-08_184500.pdf
```

* PNG soubory se po vytvoření PDF **nemažou**.
* Systémový `temp` adresář se nepoužívá.
* Výsledné PDF: jeden snímek = jedna stránka, bez rotace, bez roztažení, se
  zachovaným poměrem stran. Obrázky se vkládají **bezeztrátově** (`FlateDecode`),
  bez JPEG rekomprese – text na screenshotech zůstane ostrý.
* V PDF je navíc **vyhledatelný text z OCR** – viz níže.
* Pořadí zpracování: pořízené snímky → vymazání oblastí → OCR → PDF. Každý krok je volitelný a při selhání se přeskočí, takže
  PDF vznikne vždy.

---

## OCR – text v PDF jde označit, kopírovat a hledat

Nad každý snímek se do PDF vloží **neviditelná textová vrstva** (režim
vykreslování `3 Tr`). Stránka vypadá úplně stejně jako předtím, ale text
v ní lze označit myší, zkopírovat, exportovat i najít přes `Ctrl+F`.

OCR obstarává **engine vestavěný ve Windows** (`Windows.Media.Ocr`).
Nic se neinstaluje ani nestahuje – je součástí Windows 10/11. Dostupné jazyky
odpovídají jazykovým balíčkům ve Windows
(`Nastavení → Čas a jazyk → Jazyk a oblast`); aplikace je vypíše do průběhu
hned po spuštění.

Naměřeno na skutečné normě ČSN (52 stránek, oblast 1291×1747 px):

| | |
|---|---|
| čas OCR | 19 s (≈ 0,4 s na stránku) |
| rozpoznaných slov | 18 850 |
| nárůst velikosti PDF | +256 kB z 8,1 MB (**+3 %**) |
| obraz stránky | beze změny, bit po bitu |

Vypnout jde v **Nastavení → „Provést OCR a vložit do PDF vrstvu
s vyhledatelným textem“**.

**Selhání OCR nikdy nezabrání vzniku PDF.** Když engine chybí, není
nainstalovaný požadovaný jazyk nebo se rozpoznávání nepovede, zapíše se
důvod do logu a PDF vznikne jen s obrázky.

### Jak je textová vrstva udělaná

Používá se základní font **Courier**, jehož všechny glyfy jsou široké přesně
600/1000 em. Šířku každého slova proto stačí na jeho rámeček z OCR napasovat
přesným výpočtem horizontálního škálování (`Tz`) – bez tabulek metrik
a bez vkládání fontu do souboru. Znaky se mapují vlastním kódováním
(`/Differences` se jmény `uniXXXX`) a přikládá se `/ToUnicode` CMap, takže
kopírování vrací správnou českou diakritiku nezávisle na prohlížeči.

> Text se do stránky zapisuje **hexadecimálně** (`<41424344> Tj`), ne jako
> literální řetězec `( )`. Kódy znaků se přidělují podle četnosti od 1 výš,
> takže na hodnoty 10 a 13 vždy padne nějaké běžné písmeno – a v literálním
> řetězci PDF normalizuje konce řádků, takže by se bajt 0x0D četl jako 0x0A
> a dvojice 0x0D 0x0A by splynula v jediný. Text by se tiše rozbil
> („práce“ → „sráce“). Hexadecimální zápis žádné escapování nepotřebuje.

## Log

`captures/<relace>/scraper.log` obsahuje průběh i důvod ukončení:

```
2026-09-08 18:45:01 INFO Start snímání, oblast: x=440 y=125 w=820 h=900
2026-09-08 18:45:01 INFO Total Commander: Total Commander (x64) 11.02  [TOTALCMD64.EXE, HWND 3802944]
2026-09-08 18:45:01 INFO RDP: 192.168.1.100 – Připojení ke vzdálené ploše  [mstsc.exe, HWND 201380]
2026-09-08 18:45:10 INFO Total Commander aktivován (HWND 3802944)
2026-09-08 18:45:10 INFO Screenshot page_0001.png
2026-09-08 18:45:11 INFO RDP aktivováno (HWND 201380)
2026-09-08 18:45:11 INFO Page Down
2026-09-08 18:45:12 INFO Total Commander aktivován (HWND 3802944)
2026-09-08 18:45:12 INFO Screenshot page_0002.png
```

---

## Nastavení

Tlačítko **Nastavení** v GUI. Hodnoty se persistentně ukládají do `config.json`
vedle `.exe`.

| Parametr | Výchozí | Význam |
|---|---|---|
| Delay po aktivaci okna | 300 ms | čekání mezi `SetForegroundWindow` a ověřením |
| Delay po Page Down | 700 ms | čekání na překreslení obsahu v RDP |
| Počet potvrzení konce | 2 | kolik po sobě jdoucích nezměněných snímků znamená konec |
| Maximální počet screenshotů | 1000 | ochrana proti nekonečné smyčce |
| Počet pokusů o aktivaci okna | 3 | po vyčerpání se automatizace zastaví |
| Pauza mezi pokusy o aktivaci | 400 ms | |
| Tolerance dHash | 4 | max. Hammingova vzdálenost (0–64) |
| Tolerance průměrného rozdílu | 0.01 | max. normalizovaný průměrný rozdíl jasu |
| Tolerance podílu změněných pixelů | 0.005 | max. podíl výrazně změněných pixelů |
| DPI stránky PDF | 96 | určuje fyzický rozměr stránky |
| Šířka předlohy [mm] | 0 | 0 = vypnuto; kladná hodnota DPI dopočítá – viz níže |
| Zvětšení snímku pro PDF | 1 | 1–4×; víc vzorků na stránku, detail nepřidá |
| Doostření pro PDF [%] | 0 | 0–300; unsharp mask, rozumně 80–150 |
| Bezeztrátově zmenšit PDF | zapnuto | indexovaná paleta – viz níže |
| Zvětšení snímku pro OCR | 2 | 1–4×; drobné písmo engine rozpozná spolehlivěji |
| Adresa RDP relace | *(prázdné)* | **povinné** – podle ní se hledá okno `mstsc.exe` |
| Šířka zakládané RDP relace | 2560 | rozlišení relace, kterou založí tlačítko |
| Výška zakládané RDP relace | 3600 | určuje strop DPI: výška / 11,69" pro A4 |
| Metoda odeslání kláves | sendinput | `sendinput` / `postmessage` |
| Provést OCR | zapnuto | vyhledatelná textová vrstva v PDF |
| Jazyk OCR | cs | jazyková značka, např. `cs` nebo `en-GB` |
| Vymazané oblasti | žádné | obdélníky odstraněné ze všech stránek |
| Snímaná oblast | *(prázdné)* | `[x, y, š, v]` v pixelech okna RDP; ukládá se sama |

### Automatický odhad DPI stránky PDF

Snímky jdou do PDF bezeztrátově a v původním rozlišení, ale pevných 96 DPI dělá
ze snímku širokého 1920 px stránku širokou 508 mm. Takovou stránku každý
prohlížeč zmenší na zlomek velikosti a text vypadá rozmazaně, přestože je
v souboru ostrý.

Stačí proto do pole **Šířka předlohy [mm]** zadat, jak je snímaný dokument
doopravdy široký (A4 na výšku = 210, na šířku = 297). DPI se pak dopočítá ze
šířky snímku a má přednost před polem *DPI stránky PDF*:

```
DPI = šířka snímku v px / (šířka předlohy v mm / 25.4)
```

Například A4 nasnímaná v šířce 1600 px vyjde na 194 DPI. Stránky mají skutečnou
velikost dokumentu, „100 %“ v prohlížeči odpovídá reálné velikosti a tisk
proběhne v plném rozlišení snímku. Dialog *Nastavení* rovnou ukazuje, jaké DPI
z aktuálně vybrané oblasti vychází. Hodnota 0 chování nemění – použije se pevné
*DPI stránky PDF*. Přepočet je v [src/pdf_export.py](src/pdf_export.py)
(`dpi_for_width`) a dělá se pro každou stránku zvlášť, takže i snímky odlišných
rozměrů vyjdou ve stejné fyzické šířce.

### Zvětšení snímku (upscale)

Obě pole zvětšují snímek jen pro daný účel – uložené PNG zůstávají netknuté
a fyzická velikost stránky PDF se nemění.

**Pro OCR** (výchozí 2×) se snímek zvětší přímo při dekódování ve WinRT
(`BitmapTransform`, filtr Fant), takže nevzniká zvětšená kopie na disku ani
v paměti. Souřadnice slov pak platí ve zvětšeném rozměru a `pdf_export` je
přepočítá zpět podle `PageText.width/height`. Zvětšení nepřidá informaci, ale
posune rozhodovací práh enginu. Naměřeno na vykresleném textu (jazyk `cs`,
27 slov):

| Výška písma | bez zvětšení | 2× | 3× |
|---|---|---|---|
| 10 px | 44 % | 78 % | 85 % |
| 12 px | 96 % | 96 % | 93 % |
| 14 px | 89 % | 89 % | 96 % |
| 18 px | 96 % | 96 % | 96 % |

Od zhruba 12 px výšky písma je efekt nulový, u drobnějšího textu velký – proto
výchozí 2×. Cena je delší OCR, obrázek se v paměti zvětší na čtyřnásobek.

**Pro PDF** (výchozí 1× = vypnuto) se snímek přepočítá filtrem LANCZOS a do
stejně velkého rámce stránky se vloží víc vzorků. Detail to nepřidá – zdrojem
zůstává původní snímek – ale prohlížeč pak nezmenšuje tak hrubou předlohu a
text bývá při zobrazení hladší. Soubor roste zhruba s druhou mocninou faktoru,
takže 2× znamená několikanásobně větší PDF. Vyplatí se to zkusit teprve tehdy,
když nejde zvýšit rozlišení samotné RDP relace.

### Bezeztrátové zmenšení PDF

Snímky vzdálené plochy mívají jen několik desítek barev – text je černý, papír
bílý a mezi tím pár odstínů vyhlazení. Tři bajty na pixel jsou pak zbytečné:
stránky s nejvýš 256 barvami se ukládají s **indexovanou paletou**
(`/Indexed /DeviceRGB`), tedy jeden bajt na pixel plus tabulka barev.
Obraz zůstává **bit po bitu stejný**, barevnější stránky se uloží jako dosud.

Naměřeno na skutečném devatenáctistránkovém dokumentu 2406×3387 px:

| varianta | obrazová data | podíl | kvalita |
|---|---|---|---|
| DeviceRGB, flate-6 | 7,68 MB | 100 % | – |
| DeviceRGB, flate-9 | 7,31 MB | 95 % | bit po bitu shodné |
| **indexovaná paleta** | **5,52 MB** | **72 %** | **bit po bitu shodné** |
| PNG prediktor | 9,85 MB | 128 % | shodné, ale větší |
| JPEG q90 | 16,79 MB | 219 % | ztrátové a větší |
| CCITT G4 (1 bit) | 4,20 MB | 55 % | ztrátové, ruší vyhlazení písma |

Celé PDF vyšlo ze 7,69 MB na **5,53 MB**, tedy o 28 % méně. Paleta se uplatnila
na 15 z 19 stránek; zbylé mají barev víc a šly cestou `DeviceRGB`.

Kóduje se opatrně: kvantizace se použije jedině tehdy, když zpětný převod dá
**přesně tytéž pixely**. Jinak stránka spadne na `DeviceRGB` – kvalita má
přednost před velikostí. Cena je zhruba **1 s na stránku** navíc při tvorbě
PDF; vypnout to jde v Nastavení.

> **PNG prediktor se záměrně nepoužívá.** Je to obvyklý trik, ale tady škodí:
> naměřeno ručně mimo Pillow `None` 493 k, `Sub` 614 k, `Up` 746 k proti 482 k
> bez prediktoru. Velké jednolité plochy se komprimují líp jako dlouhé shodné
> běhy; diference je rozseká a na hranách písmen vyrobí vysokou entropii.

### Doostření (unsharp mask)

Pole **Doostření pro PDF [%]** zvýší kontrast na hranách písmen – to je jediná
úprava, která opticky vrátí část ostrosti sežrané škálováním a kodekem RDP.
Dělá se až po zvětšení a poloměr masky roste s jeho faktorem, protože tah písma
je po zvětšení širší ([`prepare_image`](src/pdf_export.py)).

Práh (`SHARPEN_THRESHOLD = 3`) drží ploché plochy beze změny, takže se
nezvýrazní šum kodeku v prázdném papíru. Rozumné hodnoty jsou 80–150; nad 200 %
vznikají kolem písmen světlé lemy a text začíná vypadat kostrbatě. Na dokonale
ostré hraně (čistá černá na čisté bílé) se nezmění nic – doostřit jde jen to,
co je rozmazané.

### Poznámka k odesílání Page Down

Výchozí `sendinput` používá Win32 API `SendInput()`. Klient Remote Desktop
nepřenáší do relace zprávy doručené přes `PostMessage`, protože klávesnici čte
ze stavu vstupu systému – `SendInput` je proto pro RDP jediná spolehlivá cesta.
Klávesa se odesílá až po ověření, že aktivním oknem je RDP relace.

Varianta `postmessage` posílá `WM_KEYDOWN`/`WM_KEYUP` přímo oknu s fokusem a je
určená pro cílové aplikace, které tento způsob přijímají.

---

## Detekce konce dokumentu

Nepoužívá se binární shoda souborů. Porovnávají se poslední dva snímky, a to
**v plném rozlišení** – viz [src/image_compare.py](src/image_compare.py).

> Porovnávat zmenšené snímky se u dokumentů nesmí: dvě různé stránky hustého
> textu mají po zmenšení prakticky stejnou šedou texturu a aplikace by ohlásila
> konec hned po první stránce. Změřeno na skutečných snímcích 2160×1130 px:
> zmenšení na 64×64 dalo pro posun o celou stránku rozdíl jen 0.012–0.018,
> zatímco v plném rozlišení vyjde 0.031–0.065.

Použité metriky:

1. **podíl výrazně změněných pixelů** – kolik pixelů se změnilo o víc než
   32 úrovní jasu (necitlivé na antialiasing),
2. **normalizovaný průměrný rozdíl jasu** – zachytí i změnu rozprostřenou
   po celé ploše,
3. **dHash** (difference hash, 64 bitů) jako doplňková pojistka na hrubou
   strukturu obrazu.

Snímky jsou „prakticky nezměněné“ jen tehdy, když se na tom shodnou **všechny**
metriky. Díky tomu blikající kurzor, hodiny, drobné animace, kurzor myši ani
změny antialiasingu konec dokumentu nevyvolají – a naopak: při pochybnostech
aplikace raději pokračuje ve snímání, než aby zkrátila výsledné PDF.

Naměřené hodnoty podílu změněných pixelů (oblast 2160×1130 px):

| situace | podíl změněných pixelů |
|---|---|
| tentýž snímek | 0.00000 |
| blikající kurzor | ~0.00002 |
| hodiny / drobná animace | ~0.002 |
| **práh pro „beze změny“** | **0.005** |
| posun dokumentu o stránku | 0.044–0.201 |
| posun jen o část stránky (konec dokumentu) | 0.015 |

Pokud by dokument obsahoval trvale běžící animaci větší než práh, aplikace by
konec nerozpoznala a zastavila by se až na maximálním počtu snímků – prahy jsou
proto v nastavení.

Konec se potvrzuje až po **dvou** po sobě jdoucích nezměněných snímcích:

```
strana A → Page Down → strana B    (uloženo)
strana B → Page Down → strana B    (potvrzení 1/2, jde do duplicates/)
strana B → Page Down → strana B    (potvrzení 2/2 → konec)
```

Duplicitní závěrečné snímky se do PDF nedostanou. Ukládají se pouze
diagnosticky do podadresáře `duplicates/` (lze vypnout v nastavení).

## Snímání PDF otevřeného v Adobe Readeru

Typické nasazení: v RDP relaci běží Adobe Reader s otevřeným PDF a aplikace
snímá **celou stránku** dokumentu, stránku po stránce.

### Nastavení Adobe Readeru

1. **Zobrazení → Zobrazení stránky → Jedna stránka** *(nikoli souvislé
   posouvání)*. `Page Down` pak přejde přesně o jednu stránku a jeden snímek
   odpovídá jedné stránce dokumentu.
2. **Zobrazení → Zvětšení → Přizpůsobit stránku** – celá stránka je vidět naráz.
3. **Zobrazení → Režim čtení** (`Ctrl+H`) skryje panely nástrojů a nechá
   dokumentu víc místa. Ostatní panely lze zavřít přes `F4`.
4. Snímanou oblast pak označte tak, aby obsahovala jen stránku dokumentu,
   bez okrajů okna a bez posuvníku.

> Chcete-li ostřejší obraz, než dovolí „Přizpůsobit stránku“, **nezvětšujte zoom** –
> zvyšte rozlišení samotné relace, viz
> [Rozlišení – jak dostat ostrý obraz](#rozlišení--jak-dostat-ostrý-obraz).
> Zvětšený zoom by znamenal, že se stránka na jeden snímek nevejde.

> **Panel s náhledy stránek ani lišty Readeru do snímané oblasti nepatří.**
> Buď je vynechte z výběru oblasti, nebo je odstraňte funkcí `Vymazat oblast…`.
> Okno Total Commanderu naopak vadit nemůže – snímá se obsah okna RDP, ne plocha.

### Vymazání oblasti ze všech stránek

Opakuje-li se na každé stránce něco, co ve výstupu nechcete – vodoznak,
hlavička, patička, číslo stránky, zbytek lišty Readeru – označte to **jednou
na první stránce** a aplikace to odstraní ze **všech** stránek. Vymazané místo
zůstane bílé.

Postup:

1. Vyberte snímanou oblast (`Vybrat oblast`).
2. Klikněte na **`Vymazat oblast…`**. Aplikace pořídí snímek první stránky
   (přes stejné ověření aktivního Total Commanderu jako při snímání) a zobrazí
   ho zmenšený.
3. Tažením myši označte, co se má vymazat. Můžete označit i více oblastí,
   tlačítko **Zpět** vezme poslední zpět, **Smazat vše** začne znovu.
4. **Použít** volbu uloží do `config.json`, takže platí i pro další spuštění.
   Počet oblastí je vidět v hlavním okně.

Souřadnice se ukládají v pixelech **snímané oblasti**, takže platí pro každou
stránku stejně. Když ještě nemáte spuštěný Total Commander, dialog nabídne
místo čerstvého snímku první stránku z poslední relace.

Maska se uplatní **před OCR**, takže se vymazaný text nedostane ani do
vyhledatelné textové vrstvy PDF.

> **Pořízená PNG zůstávají nedotčená.** Vymazané kopie vznikají vedle nich
> v podadresáři `masked/`. Když vymazání jakkoli selže, PDF se vytvoří
> z původních snímků.

---

## Rozlišení – jak dostat ostrý obraz

Kvalitu určuje jediné číslo: **kolik pixelů má stránka na výšku uvnitř relace**.
Celá A4 je vysoká 11,69". Při viewportu vysokém 2160 px z toho vyjde 185 DPI,
při 3600 px už 300 DPI. Doostření ani zvětšení chybějící detail nedoplní – v
obraze prostě není.

Výška monitoru přitom **není** strop. Okno `mstsc.exe` smí být vyšší než
obrazovka a `PrintWindow` vrátí i to, co je pod jejím okrajem.

### Co nefunguje

Roztáhnout okno už běžící relace nestačí. `mstsc` sice okno zvětší, ale
dynamicky vyjednané rozlišení relace **zastropuje velikostí fyzického monitoru**.
Naměřeno na monitoru 3840×2160:

| velikost okna | plocha relace v okně |
|---|---|
| 2560×2160 | 2560×**2160** |
| 2560×3700 | 2560×**2160** + černý pruh 757 px nahoře i dole |
| 4400×2160 | **3840**×2160 + černý pruh 280 px vlevo i vpravo |

Okno se zvětší, obrazu nepřibude ani pixel.

### Co funguje

Relaci je potřeba **založit** s vyšším rozlišením, ne ji zvětšovat za běhu.
Obstará to tlačítko **Založit RDP relaci…**: podle adresy a rozlišení
z Nastavení vygeneruje vedle `.exe` soubor `rdp_session.rdp`, spustí nad ním
`mstsc.exe` a počká, až se přihlásíte. Pak okno rovnou roztáhne na plochu
relace. Heslo ani bezpečnostní dialog aplikace neřeší – to zůstává na vás.

Vygenerovaný soubor vypadá takto:

```text
full address:s:192.168.30.10
screen mode id:i:1
desktopwidth:i:2560
desktopheight:i:3600
smart sizing:i:0
dynamic resolution:i:0
session bpp:i:32
compression:i:0
connection type:i:6
networkautodetect:i:0
bandwidthautodetect:i:0
```

Podstatné jsou `desktopwidth`/`desktopheight` (rozlišení relace),
`smart sizing:i:0` (jinak by `mstsc` obraz zmenšoval do okna a rozlišení by se
zahodilo) a `dynamic resolution:i:0` (jinak by relace opět spadla na velikost
monitoru). `compression:i:0` a LAN profil drží kodek RDP co nejblíž
bezeztrátovému – při vyšším rozlišení je to znát na ostrosti písma.
Žádné přihlašovací údaje se do souboru nezapisují.

> Když už relace k dané adrese běží, nové připojení ji **převezme**. Aplikace se
> na to předem zeptá. Otevřené aplikace na serveru zůstanou, jen se přepočítá
> plocha.

> Když server zadané rozlišení odmítne, relace se založí menší a aplikace to
> ohlásí. Starší servery odmítají výšku nad 2048 px – zkuste pak menší hodnotu.

### Nastavení prohlížeče

V Adobe Readeru pak stačí **Jedna stránka** + **Přizpůsobit stránku** a jeden
`Page Down` = jedna stránka dokumentu = jeden snímek. Žádné posouvání po částech,
žádné skládání.

## DPI a více monitorů

Aplikace zapíná **per-monitor DPI awareness v2** (`SetProcessDpiAwarenessContext`)
ještě před vytvořením prvního okna, takže `GetClientRect` i `PrintWindow` pracují
se skutečnými pixely při škálování 100 %, 125 %, 150 % i 175 %.

Na tom, kde okno RDP na ploše leží a přes který monitor je roztažené, nezáleží –
souřadnice oblasti jsou vázané na okno. Total Commander může být na jiném
monitoru, na stejném, nebo okno RDP překrývat.

## Ošetřené chybové stavy

Total Commander není spuštěn · RDP není spuštěno · není vyplněna adresa RDP relace ·
relace k zadané adrese nenalezena · RDP nebo Total Commander zavřen během běhu · okno nelze aktivovat ·
screenshot se nepodařilo pořídit · okno RDP minimalizované · snímek okna je
jednolitě černý · snímaná oblast přesahuje okno RDP (okno změnilo velikost) ·
neplatná nebo prázdná oblast · do pracovního adresáře nelze zapisovat ·
selhání tvorby PDF · přerušení uživatelem · překročení maximálního počtu snímků.

**Žádná z těchto situací nevede ke ztrátě již pořízených snímků.** PNG soubory
zůstávají na disku a PDF z nich lze kdykoliv vytvořit tlačítkem
**Vytvořit PDF nyní**.

Je-li RDP okno minimalizované, aplikace jej před spuštěním obnoví; pokud se to
nepodaří, vyzve uživatele k ručnímu obnovení a snímání nespustí.

---

## Struktura projektu

```
rdp-screenshot-scraper/
├── README.md
├── requirements.txt        přesně připnuté verze
├── build.bat               offline build .exe
├── run.bat                 offline spuštění ze zdrojáků
├── RdpScreenshotScraper.spec
├── vendor/                 všechna kolečka (.whl) – build bez internetu
├── src/
│   ├── main.py             vstupní bod, zapnutí DPI awareness
│   ├── gui.py              tkinter GUI, nastavení, výběr okna
│   ├── window_manager.py   Win32 API: hledání, aktivace, ověření, geometrie, Page Down
│   ├── capture.py          snímání okna přes PrintWindow, validace oblasti
│   ├── image_compare.py    dHash + rozdíl pixelů, detekce konce
│   ├── ocr.py              OCR přes engine vestavěný ve Windows
│   ├── mask.py             vymazání zvolené oblasti ze všech stránek
│   ├── rdp_session.py      generování .rdp a založení relace s pevným rozlišením
│   ├── pdf_export.py       bezeztrátové PDF + neviditelná textová vrstva
│   ├── automation.py       snímací cyklus, stavy, logování
│   └── config.py           config.json, pracovní adresář, adresáře relací
└── tests/
    ├── helpers.py
    ├── pdf_reader.py           minimální čtečka PDF pro testy
    ├── test_automation.py
    ├── test_config_and_capture.py
    ├── test_image_compare.py
    ├── test_mask.py
    ├── test_ocr.py
    ├── test_pdf_export.py
    ├── test_pdf_text_layer.py
    ├── test_rdp_session.py
    └── test_window_manager.py
```

## Kurzor myši

`PrintWindow` vykresluje obsah okna, kurzor myši do něj nepatří. Kurzor tedy
nemůže ovlivnit ani detekci konce dokumentu.
