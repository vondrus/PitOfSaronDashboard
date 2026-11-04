# Pit of Saron Dashboard

Interaktivní dashboard v jazyce Python (knihovny **Plotly** a **Dash**) pro datovou analýzu
týmových Mythic+ runů dungeonu **Pit of Saron** ze hry World of Warcraft. Data pocházejí
z veřejného [Raider.IO API](https://raider.io/api) (endpointy
[`mythic-plus/runs`](https://raider.io/api#/mythic_plus/getApiV1MythicplusRuns) a
[`mythic-plus/run-details`](https://raider.io/api#/mythic_plus/getApiV1MythicplusRundetails)).

Dashboard umožňuje porovnávat specializace, třídy, hráče i celá složení týmů, sledovat vývoj
v čase, hledat nejlepší a nejhorší runy a zjistit, co se hraje nejčastěji. Celé uživatelské
rozhraní je v češtině.

## Obsah dashboardu

### Rozvržení a stránky

Dashboard je rozdělen na **5 stránek**, z nichž každá se vejde na běžnou Full HD obrazovku
(1920 × 1080) bez posouvání. Vlevo je trvale zobrazen panel s globálními filtry, nahoře řada
ukazatelů (KPI) a pod ní stránkování:

| Stránka | Obsah |
| ------- | ----- |
| 1. Vývoj v čase | Vývoj v čase + Teplotní mapa |
| 2. Specializace a třídy | Žebříček + Podíl kategorií |
| 3. Časy a výkon | Rozložení časů dokončení + Item level vs. čas dokončení |
| 4. Složení týmů | Nejúspěšnější složení týmů |
| 5. Žebříček runů | tabulka nejlepších / nejhorších runů |

Mezi stránkami se přepíná **záložkami**, tlačítky **‹ Předchozí** / **Další ›** (vpravo je
čítač „Strana X z 5“) nebo šipkami **←** / **→** na klávesnici (pokud není aktivní formulářový
prvek). Filtry i nastavení grafů se při přepínání stránek zachovávají. Grafy se přizpůsobí
výšce okna; na nižších obrazovkách (např. 1366 × 768) se obsah stránky mírně posouvá
(stránkování zůstává přilepené nahoře) a na úzkých obrazovkách se filtry přesunou nad obsah.

### Grafy (7 grafů, 6 typů)

| Graf | Typ | Co ukazuje | Vlastní ovládací prvky |
| ---- | --- | ---------- | ---------------------- |
| **Vývoj v čase** | čárový | počet runů, medián/nejlepší čas, průměrná a nejvyšší úroveň klíče, item level po dnech či týdnech | metrika, rozdělení (úroveň klíče, affix, frakce, tank), granularita, kumulativně |
| **Žebříček** | sloupcový (horizontální) | nejhranější / nejrychlejší / nejúspěšnější specializace, třídy, hráči a rasy | položky, role, metrika řazení, pořadí, počet položek (posuvník), herní barvy tříd |
| **Podíl kategorií** | koláčový (donut) | zastoupení tank a healer specializací, DPS tříd, úrovní klíče, affixů, frakcí a ras | kategorie, popisky, max. počet výsečí (posuvník), herní barvy tříd |
| **Teplotní mapa** | heatmapa | aktivita podle dne v týdnu × hodiny, synergie tank × healer, DPS třídy × úroveň klíče | osy, hodnota (počet, podíl ve sloupci, medián času, průměrný klíč) |
| **Rozložení časů dokončení** | krabicový / houslový | medián, kvartily a odlehlé hodnoty času podle skupin včetně časového limitu | skupiny, typ grafu, zobrazení bodů |
| **Item level vs. čas dokončení** | bodový | vztah vybavení týmu a času, lineární trend a korelace | barva podle, max. počet bodů (posuvník), trend |
| **Nejúspěšnější složení týmů** | sloupcový (horizontální) | nejčastější a nejrychlejší kombinace tank + healer + 3 DPS | úroveň detailu, řazení, min. počet runů, počet složení |

Doplňují je **ukazatele (KPI)** – počet runů ve výběru, medián času, nejrychlejší run,
nejvyšší klíč, průměrný item level, nejhranější tank a healer – a **žebříček runů** (tabulka
nejlepších/nejhorších runů s kompletním složením týmu, řazením a volbou počtu řádků).

### Interaktivita

- **Globální filtry** (ovlivňují všechny stránky): období od–do, rozsah úrovně klíče (posuvník), frakce,
  affix týdne, třídy v týmu (všechny vybrané musí být v týmu), minimální průměrný item level
  týmu, vyhledání hráče podle části jména a tlačítko **Zrušit filtry**.
- **Stránkování** – záložky, tlačítka Předchozí/Další a klávesové šipky (viz výše).
- **Lokální ovládací prvky** u každého grafu mění jen daný graf (výběr metriky, klíče řazení,
  granularity, typu grafu, počtu položek atd.).
- **Podrobnosti po najetí myší** – všechny grafy mají české popisky; bodový graf zobrazí
  u každého runu čas, rezervu, datum, affix a celé složení týmu včetně jmen hráčů.
- Legenda grafů umožňuje řady skrývat (dvojklikem izolovat), grafy lze přibližovat a stahovat
  jako PNG.
- **Tři barevná schémata** přepínatelná v záhlaví:
  - *Ledová koruna* – tmavé modré schéma (výchozí),
  - *Saronit* – tmavé zelenofialové schéma,
  - *Denní světlo* – světlé schéma s paletou Okabe–Ito vhodnou pro barvoslepé.

### Ošetření chybových stavů

Všechny chyby a neobvyklé situace jsou uživateli srozumitelně oznámeny česky:

| Situace | Reakce |
| ------- | ------ |
| chybějící, prázdný či nečitelný datový soubor, chybějící sloupce | aplikace se nespustí a vypíše chybu a nápovědu (`--data`) |
| neplatné nebo duplicitní řádky, item level 0 | řádky jsou vynechány / hodnota je brána jako chybějící; informace v sekci „O datech“ |
| počáteční datum po koncovém, datum či úroveň klíče mimo rozsah | hodnoty jsou prohozeny/upraveny a zobrazí se upozornění |
| item level mimo rozsah dat nebo nečíselný | filtr se nepoužije, zobrazí se upozornění s platným rozsahem |
| žádná vybraná frakce, více než 5 požadovaných tříd, nenalezený hráč | chybové hlášení; grafy, KPI i tabulka ukazují „žádná data“ |
| velmi malý výběr | upozornění na nespolehlivost statistik |
| číselné vstupy mimo povolený rozsah (min. počet runů, počet řádků…) | hodnota je omezena na rozsah a zobrazí se upozornění |
| nesmyslná kombinace voleb (např. kumulativní medián) | volba se ignoruje a graf vysvětlí proč |
| příliš mnoho bodů k vykreslení, málo dat pro trend či buňku heatmapy | omezení a informační poznámka u grafu |
| neočekávaná chyba při vykreslení grafu | ostatní části dashboardu fungují dál, u grafu se zobrazí chyba (podrobnosti v logu) |
| obsazený port nebo neplatná adresa, port mimo 1–65535, neznámá časová zóna | srozumitelná chyba při spuštění |

## Požadavky

- Python **3.11** nebo novější
- knihovny `dash` (≥ 4.0), `plotly` (≥ 6.0), `pandas` (≥ 2.2), `numpy`
  (na Windows navíc `tzdata` – instaluje se automaticky)

## Instalace a spuštění

### Varianta A – [uv](https://docs.astral.sh/uv/) (doporučeno)

```bash
uv run pos_dashboard.py
```

`uv` podle `pyproject.toml` a `uv.lock` samo vytvoří virtuální prostředí a nainstaluje
závislosti.

### Varianta B – pip a virtuální prostředí

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python pos_dashboard.py
```

Alternativně lze projekt nainstalovat jako balíček (`pip install .`) a spouštět příkazem
`pos-dashboard --data cesta/k/pos_data.csv`.

Po spuštění otevřete v prohlížeči adresu <http://127.0.0.1:8050/>. Server se ukončí
klávesami `Ctrl+C`.

## Parametry příkazového řádku

| Parametr | Výchozí hodnota | Popis |
| -------- | --------------- | ----- |
| `-d`, `--data CESTA` | `pos_data.csv` ve složce aplikace | cesta k CSV souboru s runy |
| `--host ADRESA` | `127.0.0.1` | adresa, na které server naslouchá (`0.0.0.0` = všechna rozhraní) |
| `-p`, `--port PORT` | `8050` | TCP port serveru (1–65535) |
| `-s`, `--scheme SCHÉMA` | `frost` | výchozí barevné schéma: `frost`, `saronite`, `light` |
| `-t`, `--timezone ZÓNA` | `Europe/Prague` | časová zóna (IANA) pro data, dny v týdnu a hodiny |
| `--debug` | vypnuto | vývojový režim Dash (jen pro lokální vývoj) |
| `--version` | – | vypíše verzi aplikace |
| `-h`, `--help` | – | vypíše nápovědu |

Příklad:

```bash
python pos_dashboard.py --data data/pos_data.csv --port 8080 --scheme light --timezone UTC
```

## Data

Soubor `pos_data.csv` obsahuje jeden řádek na každý dokončený run (19 692 runů, sezóna
Midnight 1, 27. 3. – 28. 4. 2026, úrovně klíče +16 až +22). Aplikace využívá zejména sloupce:

| Sloupec | Význam |
| ------- | ------ |
| `keystone_run_id` | identifikátor runu na Raider.IO |
| `completed_at` | čas dokončení (UTC) |
| `mythic_level` | úroveň klíče |
| `keystone_time_ms`, `clear_time_ms` | časový limit a skutečný čas dokončení (ms) |
| `faction` | frakce týmu (`mixed`, `alliance`, `horde`) |
| `modifier_1` | primární affix týdne (Fortified / Tyrannical) |
| `{slot}_name`, `_race`, `_class`, `_spec`, `_ilvl` | údaje o členech týmu; `slot` = `tank`, `healer`, `dps_1`–`dps_3` |

Při načtení se data vyčistí (neplatné a duplicitní řádky se vynechají, item level 0 se považuje
za chybějící údaj) a dopočítají se odvozené veličiny: čas v minutách, rezerva do limitu,
průměrný item level týmu, den, týden a hodina ve zvolené časové zóně, složení týmu nezávislé
na pořadí DPS atd.

## Architektura

Celá aplikace je záměrně v jediném souboru [`pos_dashboard.py`](pos_dashboard.py), který je
ale rozdělen do nezávislých vrstev:

1. **Konfigurace** – konstanty, české popisky, metriky a tři barevná schémata
   (`ColorScheme` generuje jak Plotly šablonu, tak CSS proměnné stránky).
2. **Formátování** – česká čísla, procenta, data a časy ve tvaru `m:ss`.
3. **Datová vrstva** (jen pandas) – načtení a validace CSV (`load_run_data`), validace
   filtrů (`build_filter`), filtrování (`select_runs`), agregace, KPI a tabulka runů.
4. **Tvorba grafů** (jen Plotly, bez závislosti na Dash) – čisté funkce `build_*_figure`,
   které z výběru dat a schématu vrací graf a případné poznámky pro uživatele.
5. **Rozvržení Dash** – skladba karet, ovládacích prvků, KPI a tabulky; rozdělení karet do
   stránek určuje jediná konstanta `PAGES`.
6. **Callbacky Dash** – propojení ovládacích prvků s datovou vrstvou a tvorbou grafů
   (s mezipamětí filtrovaných výběrů) a přepínání stránek.
7. **Rozhraní příkazového řádku** – `argparse` a funkce `main`.

Komentáře a docstringy ve zdrojovém kódu jsou v angličtině.

## Testy

```bash
uv run --group dev pytest
# nebo: pip install pytest && python -m pytest
```

Testy ověřují načítání a čištění dat, validaci filtrů a vstupů, všechny kombinace voleb
u všech grafů, chování při prázdném výběru, stránkování i rozhraní příkazového řádku.

## Struktura projektu

``` plaintext
PitOfSaronDashboard/
├── pos_dashboard.py          # aplikace (data, grafy, Dash, CLI)
├── pos_data.csv              # data z Raider.IO
├── tests/
│   └── test_pos_dashboard.py # automatické testy
├── pyproject.toml            # metadata projektu a závislosti
├── uv.lock                   # zamčené verze závislostí pro uv
├── requirements.txt          # závislosti pro pip
├── pos_spec.txt              # zadání projektu
├── pos_static_example.html   # prvotní statický návrh (inspirace)
├── LICENSE
└── README.md
```

## Informační zdroje

- [Pit of Saron – Warcraft Wiki](https://warcraft.wiki.gg/wiki/Pit_of_Saron)
- [Pit of Saron – Wowhead](https://www.wowhead.com/zone=4813/pit-of-saron)
- [Plotly pro Python](https://plotly.com/python/)
- [Dash](https://dash.plotly.com/)

## Autoři a správci

- Daniel Abraham – <daniel.abraham@unicornuniversity.net>
- Ondřej Janeček – <ondrej.janecek@unicornuniversity.net>
- Petr Vondrus – <vondrus.petr@gmail.com>

## Licence

Projekt je šířen pod licencí [MIT](LICENSE).

World of Warcraft je ochranná známka společnosti Blizzard Entertainment. Projekt není
s Blizzard Entertainment ani Raider.IO nijak spojen.
