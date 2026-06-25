# Energi Dashboard

Live dashboard der viser strømforbrug, lys og elpriser.  
Data hentes fra Shelly-enheder og Eloverblik, og vises på en statisk GitHub Pages side.

## Hvad det viser

- Aktuelt strømforbrug (W/kW) — opdateres hvert minut
- Hvilke lys er tændt, brightness og watt
- Forbrug i går og denne måned (kWh)
- 7 dages forbrug med pris i kr — klik på en dag for timevisning
- Spotpris (øre/kWh) og total pris inkl. afgifter per time

## Arkitektur

```
[Shelly enheder]  ──┐
                    ├──▶  pusher/push_data.py  ──▶  GitHub Gist (data.json)
[Eloverblik API]  ──┘                                      │
[Energi Data Service]                                      ▼
                                                   GitHub Pages (index.html)
                                                   henter fra gisten hvert minut
```

- **Lokal Linux-kasse** kører `push_data.py` hvert minut via cron
- **GitHub Gist** bruges som datalagringssted (ingen commits, ingen git-historik)
- **GitHub Pages** (statisk site) henter og viser data fra gisten

---

## Opsætning

### 1. GitHub Gist

Opret en tom public gist på [gist.github.com](https://gist.github.com) med filen `data.json` (indhold: `{}`).  
Notér gist-id'et — de 32 hex-tegn i URL'en.

### 2. GitHub Personal Access Token

Gå til [github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta) og opret et token med scope **Gists: Read and write**.

### 3. Konfiguration

Udfyld `pusher/config.yaml`:

```yaml
github:
  token: "github_pat_..."        # dit token
  gist_id: "abc123..."           # 32-tegns gist-id

eloverblik:
  token: "eyJhbGci..."           # JWT refresh token fra eloverblik.dk → Min profil → Datadeling
  metering_point: "571313..."    # 18-cifret målepunktsnummer

price_area: "DK1"                # DK1 = Jylland/Fyn, DK2 = Sjælland

tariffs:
  elafgift:           0.761      # DKK/kWh ekskl. moms (opdater ved årsskifte)
  systemtarif:        0.054
  transmissionstarif: 0.049
  dso_tarif:          0.0        # Dit netselskabs tarif — find på elregningen
  moms:               0.25

shelly_devices:
  - name: "Stue loft"
    ip: "192.168.1.100"
    gen: 1          # 1 = Gen1 (Dimmer, 1PM, 2.5), 2 = Gen2 (Plus, Pro)
    type: "dimmer"  # dimmer / relay / switch
```

> **Vigtigt:** `config.yaml` er i `.gitignore` og committes aldrig — den indeholder tokens.

### 4. Installer afhængigheder (én gang)

```bash
cd pusher
pip3 install -r requirements.txt
```

### 5. Test scriptet

```bash
python3 push_data.py
```

Du bør se noget lignende:
```
15:30:00 INFO Eloverblik daily: henter
15:30:08 INFO History: henter 7 dage
15:30:14 INFO Gist: OK
```

### 6. Cron job (kører hvert minut)

```bash
crontab -e
```

Tilføj linjen:
```
* * * * * cd /Users/tombramsen/web/web/pusher && python3 push_data.py >> /tmp/energi.log 2>&1
```

### 7. GitHub Pages

1. Push `docs/` til dit GitHub repo
2. Gå til repo → **Settings → Pages → Branch: main → Folder: /docs → Save**
3. Siden er tilgængelig på `https://BRUGERNAVN.github.io/REPO/`

---

## Filer

```
energi-dashboard/
├── docs/
│   ├── index.html          # Dashboard (deploy til GitHub Pages)
│   └── data.json           # Kun til lokal test — live data kommer fra gisten
├── pusher/
│   ├── push_data.py        # Hovedscript
│   ├── config.yaml         # Din konfiguration (gitignored)
│   └── requirements.txt
└── README.md
```

## Shelly-modeller

| Model | gen | type |
|---|---|---|
| Shelly Dimmer 1/2 | 1 | dimmer |
| Shelly 1, 1PM, 2.5 | 1 | relay |
| Shelly Plug S | 1 | relay |
| Shelly Plus 1, Plus 1PM | 2 | switch |
| Shelly Pro 1/2/4 | 2 | switch |
| Shelly Plus Dimmer | 2 | dimmer |

## Prisberegning

Total pris = `(spotpris + elafgift + systemtarif + transmissionstarif + dso_tarif) × 1,25 moms`

Spotpriser hentes fra [Energi Data Service](https://www.energidataservice.dk/) (gratis, ingen auth).  
`dso_tarif` varierer per netselskab og tidspunkt — find din tarif på elregningen eller netselskabets hjemmeside.
