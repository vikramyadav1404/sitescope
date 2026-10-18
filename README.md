# SiteScope

A free technical SEO audit tool. Enter a website; SiteScope crawls it, checks every page, and ranks the issues by
**Priority = Severity × % of pages affected**.

CSET489 mini-project, Bennett University — Vikram Yadav (E23CSEU2360).

## What it checks

| Module | What it does |
|---|---|
| Async crawler (`app/crawler.py`) | Breadth-first crawl with asyncio + httpx, robots.txt, XML sitemaps, rate limiting, URL normalisation |
| On-page checks (`app/checks.py`) | Status codes, redirect chains, noindex, titles, meta descriptions, H1, canonical, alt text, lang, thin content, broken links |
| Link graph (`app/linkgraph.py`) | NetworkX internal link graph: PageRank, click depth, orphan pages |
| Duplicates (`app/simhash.py`) | 64-bit SimHash fingerprints flag duplicate and near-duplicate pages |
| Core Web Vitals (`app/vitals.py`) | LCP, INP, CLS from the PageSpeed Insights API (CrUX field data, else Lighthouse lab data) |
| JS rendering diff (`app/vitals.py`) | Optional: compares raw HTML with a Playwright-rendered page (`RENDER_JS=1`, Playwright installed) |
| Safety (`app/safety.py`) | Blocks audits of private addresses (localhost, the VM, 169.254.169.254) |

Stack: Python 3.12, FastAPI, Jinja2 templates, PostgreSQL 16, Docker Compose, Nginx, Cloudflare.

## Run locally

```bash
pip install -r requirements-dev.txt
pytest -q                                  # 8 tests, no network needed
uvicorn app.main:app --reload              # http://127.0.0.1:8000 (in-memory store)
```

With Docker and PostgreSQL:

```bash
cp .env.example .env                       # set POSTGRES_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

## Deploy on Azure (Azure for Students) behind Cloudflare

Take a screenshot at each step marked 📸 — the report needs them (Sections 7–8).

### 1. Put the code on GitHub
Create an empty repo `sitescope` on github.com, then on your laptop:
```bash
cd sitescope
git init && git add . && git commit -m "SiteScope v1"
git branch -M main
git remote add origin https://github.com/vikramyadav1404/sitescope.git
git push -u origin main
```

### 2. Create the VM (Azure portal) 📸
portal.azure.com → **Virtual machines → Create**
- Subscription: *Azure for Students* · Resource group: new `sitescope-rg`
- Name `sitescope-vm` · Region **Central India** · Image **Ubuntu Server 24.04 LTS**
- Size **B1s** (or B2ats_v2 / B2s if B1s is unavailable)
- Authentication **SSH public key**, username `azureuser`, download the `.pem` key
- Inbound ports: **SSH (22), HTTP (80), HTTPS (443)**
- Networking → Public IP → set to **Static** (so DNS never breaks)

Note the **public IP**.

### 3. Prepare the server
```bash
chmod 400 ~/Downloads/sitescope-vm_key.pem
ssh -i ~/Downloads/sitescope-vm_key.pem azureuser@<PUBLIC_IP>

sudo apt update && sudo apt upgrade -y
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp
sudo ufw --force enable && sudo ufw status
# 1 GB of swap helps on a 1 GB B1s
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
curl -fsSL https://get.docker.com | sudo sh
docker --version && docker compose version
```

### 4. Start SiteScope
```bash
git clone https://github.com/vikramyadav1404/sitescope.git
cd sitescope && cp .env.example .env
nano .env        # set a long POSTGRES_PASSWORD; add PAGESPEED_API_KEY (free, Google Cloud Console)
sudo docker compose up -d --build
sudo docker compose ps
curl http://127.0.0.1:8000/health
```

### 5. Nginx
```bash
sudo apt install -y nginx
sudo cp deploy/nginx-sitescope.conf /etc/nginx/sites-available/vikramyadav.me
sudo ln -s /etc/nginx/sites-available/vikramyadav.me /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 6. Cloudflare DNS 📸
vikramyadav.me is already added to Cloudflare (Free plan). In **DNS → Records**:
1. **Add** `CNAME quadrantcode → 2ec3f6ab8e29d766.vercel-dns-017.com`, **DNS only (grey)**. (`nexora` is already there.) Do this *before* step 3 below or quadrantcode.vikramyadav.me will stop working.
2. **Edit** the four `A vikramyadav.me → 185.199.10x.153` (GitHub Pages) records: change one to the VM's public IP and delete the other three. Set it to **DNS only (grey)** for now. Keep `CNAME www → vikramyadav.me`, also grey for now.
3. **Namecheap** → Domain List → vikramyadav.me → Manage → Nameservers → **Custom DNS**:
   `junade.ns.cloudflare.com`, `magnolia.ns.cloudflare.com` → ✓ save. 📸
4. Wait until Cloudflare emails "vikramyadav.me is now active" (minutes to a few hours). Check: `dig +short NS vikramyadav.me`.

### 7. SSL (Full Strict) 📸
On the VM (records still grey, so Let's Encrypt reaches the server directly):
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d vikramyadav.me -d www.vikramyadav.me
sudo certbot renew --dry-run
```
Then in Cloudflare: turn the `vikramyadav.me` A record and `www` CNAME to **Proxied (orange)**, and set
**SSL/TLS → Overview → Full (strict)**. Leave `nexora` and `quadrantcode` grey (Vercel needs DNS only).

### 8. Check and collect evidence 📸
```bash
curl -I https://vikramyadav.me
cd ~/sitescope && bash deploy/evidence.sh vikramyadav.me | tee evidence.txt
```
`evidence.txt` has the OS, Nginx, Docker and PostgreSQL versions, firewall, DNS, headers and certificate expiry for
Sections 7–8 and Appendix A. Then run an audit on https://vikramyadav.me and screenshot the report.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | empty (memory) | PostgreSQL DSN; set automatically in Docker Compose |
| `POSTGRES_PASSWORD` | — | Database password (Compose) |
| `PAGESPEED_API_KEY` | empty | Google PageSpeed Insights API key; without it Google often returns HTTP 429 |
| `SITE_URL` | https://vikramyadav.me | Used for canonical tags and the sitemap |
| `MAX_PAGES` | 300 | Pages per audit |
| `CONCURRENCY` / `REQUEST_DELAY_MS` | 5 / 200 | Crawl politeness |
| `RENDER_JS` | off | `1` enables the Playwright rendering diff (needs Playwright + Chromium) |

## API

`POST /api/audits {"url": "example.com", "max_pages": 50}` → `{"id": ..., "report_url": ...}`
`GET /api/audits/{id}` → status and full JSON result. Interactive docs at `/api/docs`.
