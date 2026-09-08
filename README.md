# RDP Screenshot Scraper

Windows aplikace, která automaticky pořizuje screenshoty zvolené oblasti obrazovky
během práce s **již spuštěnou a přihlášenou** RDP relací, posouvá dokument klávesou
`Page Down`, sama rozpozná konec dokumentu a ze snímků sestaví jedno PDF.

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

Aplikace nepotřebuje `pywin32` – práce s Win32 API je řešena přes `ctypes` ze
standardní knihovny. Za běhu jsou potřeba pouze `mss` (screenshoty) a
`Pillow` (obrázky). GUI staví na `tkinter`, který je součástí instalace Pythonu.

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

1. Spusť **Total Commander**.
2. Spusť a přihlas se přes **RDP** k svému serveru.
   *(Aplikace RDP relaci sama nenavazuje.)*
3. Otevři v RDP dokument na první pozici.
4. Spusť **RDP Screenshot Scraper**.
5. V **Nastavení** vyplň **adresu RDP relace** (viz podmínka výše).
6. Klikni **Vybrat oblast**.
7. Tažením myši označ oblast dokumentu (`ESC` výběr zruší).
8. Klikni **Spustit**.
9. Aplikace postupně pořídí screenshoty.
10. Po dosažení konce dokumentu vytvoří výsledné PDF s vyhledatelným textem.

> **Upozornění:** Během automatického snímání uživatel nemá ručně měnit obsah
> RDP dokumentu ani zavírat Total Commander nebo RDP relaci.

### Rozmístění oken

Protože se snímá výhradně tehdy, když je vpředu **Total Commander**, musí být
snímaná oblast RDP relace v tu chvíli stále vidět. Okno Total Commanderu ji
nesmí překrývat. V praxi to znamená jedno z:

* RDP relace na jednom monitoru, Total Commander na druhém *(doporučeno)*,
* nebo obě okna vedle sebe tak, aby se snímaná oblast nepřekrývala.

Pokud by Total Commander snímanou oblast zakryl, aplikace bude korektně snímat –
ale výsledkem budou snímky Total Commanderu, ne dokumentu. Po prvním spuštění se
proto vyplatí zkontrolovat `page_0001.png` v adresáři relace.

Pokud během běhu omylem aktivuješ jiné okno, aplikace v dalším kroku správné
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
        scraper.log
        RDP_capture_2026-09-08_184500.pdf
```

* PNG soubory se po vytvoření PDF **nemažou**.
* Systémový `temp` adresář se nepoužívá.
* Výsledné PDF: jeden snímek = jedna stránka, bez rotace, bez roztažení, se
  zachovaným poměrem stran. Obrázky se vkládají **bezeztrátově** (`FlateDecode`),
  bez JPEG rekomprese – text na screenshotech zůstane ostrý.
* V PDF je navíc **vyhledatelný text z OCR** – viz níže.

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
| Adresa RDP relace | *(prázdné)* | **povinné** – podle ní se hledá okno `mstsc.exe` |
| Metoda odeslání Page Down | sendinput | `sendinput` / `postmessage` |
| Provést OCR | zapnuto | vyhledatelná textová vrstva v PDF |
| Jazyk OCR | cs | jazyková značka, např. `cs` nebo `en-GB` |

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

## DPI a více monitorů

Aplikace zapíná **per-monitor DPI awareness v2** (`SetProcessDpiAwarenessContext`)
ještě před vytvořením prvního okna – souřadnice výběru proto odpovídají skutečným
pixelům při škálování 100 %, 125 %, 150 % i 175 %.

Překryv pro výběr oblasti pokrývá celou virtuální plochu včetně **záporných
souřadnic** monitorů umístěných vlevo od primárního. Total Commander a RDP mohou
být na různých monitorech.

## Ošetřené chybové stavy

Total Commander není spuštěn · RDP není spuštěno · není vyplněna adresa RDP relace ·
relace k zadané adrese nenalezena · RDP nebo Total Commander zavřen během běhu · okno nelze aktivovat ·
screenshot se nepodařilo pořídit · neplatná nebo prázdná oblast · do pracovního
adresáře nelze zapisovat · selhání tvorby PDF · přerušení uživatelem ·
překročení maximálního počtu snímků.

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
│   ├── window_manager.py   Win32 API: hledání, aktivace, ověření, Page Down
│   ├── capture.py          screenshot oblasti (mss), validace oblasti
│   ├── region_selector.py  fullscreen overlay pro výběr oblasti myší
│   ├── image_compare.py    dHash + rozdíl pixelů, detekce konce
│   ├── ocr.py              OCR přes engine vestavěný ve Windows
│   ├── pdf_export.py       bezeztrátové PDF + neviditelná textová vrstva
│   ├── automation.py       snímací cyklus, stavy, logování
│   └── config.py           config.json, pracovní adresář, adresáře relací
└── tests/
    ├── helpers.py
    ├── pdf_reader.py           minimální čtečka PDF pro testy
    ├── test_automation.py
    ├── test_config_and_capture.py
    ├── test_image_compare.py
    ├── test_ocr.py
    ├── test_pdf_export.py
    ├── test_pdf_text_layer.py
    ├── test_region_selector.py
    └── test_window_manager.py
```

## Kurzor myši

Snímky vznikají přes `mss` (BitBlt nad desktop DC), který kurzor myši
nezachycuje. Kurzor tedy nemůže ovlivnit ani detekci konce dokumentu.
