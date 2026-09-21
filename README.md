# radtest – NVMe lemezek vizsgálata protonbesugárzás után

Cél: eldönteni, hogy a besugárzott lemez **fizikailag** (NAND, vezérlő) vagy csak
**szoftveresen/logikailag** (firmware, FTL, metaadat) sérült-e.

## Használat

```bash
make                           # bin/nvblk fordítása (egyszer)
./radtest.py watch             # hot-plug: bedugod a lemezt -> teszt -> "kivehető" üzenet
./radtest.py watch --yes       # ugyanez rákérdezés nélkül
./radtest.py status            # másik terminálból: hol tart a teszt
./radtest.py report            # ha minden lemez kész: results/REPORT.{txt,md,csv}
./radtest.py run nvme0         # egy már bent lévő lemez tesztelése
```

A kezelő minden lépésről értesítést kap a konzolon, írás/olvasás közben 30 másodpercenként
százalékkal, sebességgel, hátralévő idővel és az addig talált hibákkal.
A már tesztelt (sikeresen befejezett) lemezt a program felismeri és kihagyja (`--retest`).
Csak PCIe NVMe lemezekkel dolgozik, és a használatban lévő (mountolt, LVM stb.) lemezekhez nem nyúl.

Hasznos opciók: `--no-extended` (kiterjesztett önteszt kihagyása), `--no-erase`,
`--limit-lbas N` (próbafutás az első N blokkon), `--slow-factor 10` / `--slow-min-ms 20`
(mikor számít lassúnak egy parancs), `--report-every 30`.

## Web felület

```bash
./web.py                       # http://<gép-IP>:8090/  (alap: minden interfészen, 8090-es port)
./web.py --bind 127.0.0.1      # csak helyben / SSH tunnelen át
```

Csak megjelenítés, tesztet nem indít és nem állít le. Nincs jelszó: aki eléri a portot, látja az eredményeket.
- **Élő teszt:** lemez adatai, 11 lépés állapota, folyamatjelző, sebesség, hátralévő idő, hibaszámlálók,
  élő késleltetési grafikon, napló.
- **Lemezek:** minden futás táblázatban; kattintásra részletek (SMART előtte/utána, lépések, grafikonok,
  lassú parancsok, summary.txt, letölthető fájlok).
- **Riport:** összesítő újragenerálása és letöltése (TXT / MD / CSV).

## Szolgáltatásként (systemd)

Mindkét rész szolgáltatásként fut, és újraindítás után magától elindul:

```bash
cp radtest-watch.service radtest-web.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now radtest-watch radtest-web
systemctl status radtest-watch        # állapot
systemctl stop radtest-watch          # leállítás (a kernel paramétereket visszaállítja)
journalctl -u radtest-watch -f        # vagy: tail -f results_watch.out
```

A `radtest-watch` a `--yes` kapcsolóval fut, mert szolgáltatásként nincs kivel megerősíttetni
a törlést: **minden bedugott, még nem tesztelt NVMe lemezt automatikusan letöröl és letesztel.**
Leállításkor SIGINT-et kap, így a futó tesztet lezárja és a kernel paramétereket visszaállítja.

## Lépések lemezenként

| # | Lépés | Mit ad |
|---|---|---|
| 1 | Azonosítás, logok | gyártó (VID/OUI), típus, S/N, FW, méret, EUI64/NGUID; SMART: üzemóra, bekapcsolások, írt/olvasott adat, kopás, tartalék, média hibák; teljes hibanapló (státusz + LBA); öntesztnapló, FW napló, perzisztens eseménynapló, gyártói logok (OCP / Intel / Solidigm / Micron / WDC / SanDisk / Kioxia), minden gyártói log page nyersen, feature-ök, regiszterek, lspci, AER számlálók |
| 2–3 | Rövid és kiterjesztett önteszt | a lemez saját diagnosztikája, hibás szegmens / LBA |
| 4 | 0. menet: eredeti tartalom olvasása | a besugárzás után olvashatatlan blokkok **felülírás előtt** |
| 5–6 | 1. menet: teljes írás + visszaolvasás | írási / olvasási hibák blokkra pontosan, tartalmi eltérések |
| 7 | Logok | változás az 1. menet alatt |
| 8 | Törlés (sanitize block erase → format) + ellenőrző olvasás | megmaradt-e régi tartalom (FTL/firmware hiba) |
| 9–10 | 2. menet: teljes írás + visszaolvasás | megmaradnak-e a hibák törlés után |
| 11 | Záró logok, rövid önteszt, telemetria, kiértékelés | SMART különbségek, összefoglaló; a telemetria a legvégén fut, mert egy besugárzott lemezt lefagyasztott (ez is eredményként rögzül) |

Az írás/olvasás a `bin/nvblk` eszközzel megy, **NVMe passthrough** parancsokkal (a kernel
blokkrétegét, page cache-t, újrapróbálkozást kikerülve), a lemez saját írási cache-e (VWC)
kikapcsolva. Minden blokk egyedi mintát kap (LBA + seed + ellenőrizhető adat), így az
ellenőrzés megkülönbözteti:
`io_error` (NVMe hibastátusz), `corrupt` (bithiba, bitszámmal), `misdirected` (más LBA tartalma,
eltolással), `stale_*` (korábbi menet / törlés után megmaradt tartalom), `zeroed`, `all_ff`,
`ctrl_error` (timeout / reset). Hiba esetén a darabot felezve blokk szintig bontja le.
Minden parancs késleltetése naplózva van. Kiugrónak számít, ha > 10× a futó átlag
(és > 20 ms): ilyenkor a lemez valószínűleg a háttérben dolgozik.

## Kiértékelés

| Eredmény | Feltétel (bármelyik) |
|---|---|
| **FIZIKAI KÁROSODÁS** | hibák a törlés utáni 2. menetben is; önteszt hibás szegmens / végzetes hiba; SMART média hibaszám nőtt; tartalék csökkent / küszöb alatt; kritikus figyelmeztetés (megbízhatóság, csak olvasható) |
| **SZOFTVERES/LOGIKAI** | az 1. menet hibái a törlés után megszűntek; törlés után megmaradt tartalom; eredeti tartalom olvashatatlan, de írás után minden jó; hibás azonosító adatok; megváltozott méret |
| **GYANÚS** | tesztek tiszták, de voltak lassú parancsok, vezérlő resetek, AER hibák, sikertelen törlés |
| **NEM MŰKÖDIK** | nem indul (PCIe-n látszik, de a driver nem inicializálja), vagy eltűnt a teszt alatt |
| **ÉP** | semmi fenti |

A `report` mindig a nyers adatokból értékel újra, így a szabályok utólag is finomíthatók.

## Eredmények

```
results/
  radtest.log                      minden kezelői üzenet
  REPORT.txt / REPORT.md / REPORT.csv
  <S/N>/<időbélyeg>/
    summary.txt, summary.json      lemez összefoglaló
    run.log                        a teszt üzenetei
    logs/01_pre, 02_after_pass1, 03_post   nyers logok (json/txt/bin)
    pass0/ pass1/ erase/ pass2/    <lépés>_errors.csv, _slow.csv, _latency.csv, _summary.json
    kernel.log, kernel_drive.log
  NOT_ENUMERATED_<PCI cím>/...     el sem induló lemezek
```

## Kernel beállítások

A driver a Linux beépített `nvme` drivere. A `radtest` futása idejére a következő
`nvme_core` paraméterek átállnak (kilépéskor visszaállnak; csak az **utána bedugott**
lemezekre hatnak):
`default_ps_max_latency_us=0` (APST ki), `max_retries=0`, `io_timeout=60`, `admin_timeout=120`.
Az nvblk parancsonként 60 mp időkorlátot ad; vezérlő reset után megvárja, míg a lemez
újra él, egyszer újrapróbálja, és ha a lemez eltűnik, a teszt „NEM MŰKÖDIK” eredménnyel zárul.

A boot parancssorban (`/proc/cmdline`) beállított `nvme_core.io_timeout=3`,
`admin_timeout=5` stb. nem módosul.

## Kipróbálás valódi lemez nélkül

```bash
tests/loopdisk.sh up        # szoftveres NVMe lemez (nvme-loop) két hibás tartománnyal
./radtest.py run nvmeX --transport loop --yes
tests/loopdisk.sh down
```
