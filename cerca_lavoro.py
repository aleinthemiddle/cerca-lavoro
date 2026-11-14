"""
Cerca Lavoro - avvisa sul PC e sul telefono quando escono nuovi annunci di lavoro
a Torino, Orbassano, Beinasco e Piossasco.

Uso:
    python cerca_lavoro.py               controlla in continuo (ogni N minuti, vedi config.json)
    python cerca_lavoro.py --una-volta   fa un solo controllo ed esce
    python cerca_lavoro.py --test        invia una notifica di prova a PC e telefono
"""
import html
import json
import logging
import os
import random
import re
import secrets
import socket
import sys
import time
import unicodedata
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

CARTELLA = Path(__file__).resolve().parent
FILE_CONFIG = CARTELLA / "config.json"
FILE_PRIVATO = CARTELLA / "privato.json"
NEL_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
PAGINA_URL = os.environ.get("PAGINA_URL", "")  # indirizzo della pagina annunci online
FILE_DATI = CARTELLA / "annunci_visti.json"
FILE_PAGINA = CARTELLA / "annunci.html"
FILE_LOG = CARTELLA / "log.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
}
PAUSA_TRA_RICHIESTE = (1.0, 2.0)  # secondi, per non sovraccaricare i siti
GIORNI_DA_TENERE = 45

log = logging.getLogger("cerca_lavoro")


# ----------------------------------------------------------------------------- utilità

def normalizza(testo: str) -> str:
    """minuscolo, senza accenti, spazi singoli"""
    testo = unicodedata.normalize("NFKD", testo or "")
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", testo.lower()).strip()


def pulisci(testo: str) -> str:
    testo = html.unescape(re.sub(r"<[^>]+>", " ", testo or ""))
    return re.sub(r"\s+", " ", testo).strip()


BLOCCATI = set()  # siti che in questo ciclo hanno risposto "troppe richieste"


def scarica(url: str) -> str | None:
    time.sleep(random.uniform(*PAUSA_TRA_RICHIESTE))
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 429:
            BLOCCATI.add(re.sub(r"^https?://([^/]+).*", r"\1", url))
        if r.status_code != 200:
            log.warning("HTTP %s su %s", r.status_code, url)
            return None
        return r.text
    except requests.RequestException as e:
        log.warning("Errore di rete su %s: %s", url, e)
        return None


def nuovo_annuncio(fonte, id_, titolo, azienda, luogo, url, descrizione="", data=""):
    return {
        "id": f"{fonte}:{id_}",
        "fonte": fonte,
        "titolo": pulisci(titolo),
        "azienda": pulisci(azienda),
        "luogo": pulisci(luogo),
        "url": url,
        "descrizione": pulisci(descrizione)[:3000],
        "data": data,
    }


# ----------------------------------------------------------------------------- portali
# Ogni funzione riceve (parola_da_cercare, comune) e restituisce una lista di annunci.

def fonte_linkedin(parola, comune):
    # Un raggio di 15 miglia attorno a Torino copre già Orbassano, Beinasco e Piossasco
    if comune != "Torino":
        return []
    url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
           f"?keywords={quote_plus(parola)}&location=Torino%2C%20Piemonte%2C%20Italia"
           "&distance=15&sortBy=DD&f_TPR=r604800&start=0")
    pagina = scarica(url)
    if not pagina:
        return []
    s = BeautifulSoup(pagina, "html.parser")
    risultati = []
    for card in s.select("div.base-search-card"):
        urn = card.get("data-entity-urn", "")
        id_ = urn.rsplit(":", 1)[-1]
        link = card.select_one("a.base-card__full-link")
        titolo = card.select_one(".base-search-card__title")
        if not (id_ and link and titolo):
            continue
        azienda = card.select_one(".base-search-card__subtitle")
        luogo = card.select_one(".job-search-card__location")
        data = card.select_one("time")
        risultati.append(nuovo_annuncio(
            "LinkedIn", id_, titolo.get_text(), azienda.get_text() if azienda else "",
            luogo.get_text() if luogo else "", link["href"].split("?")[0],
            data=data.get("datetime", "") if data else ""))
    return risultati


def dettaglio_linkedin(annuncio):
    id_ = annuncio["id"].split(":", 1)[1]
    pagina = scarica(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id_}")
    if pagina:
        s = BeautifulSoup(pagina, "html.parser")
        d = s.select_one(".show-more-less-html__markup") or s.select_one(".description__text")
        if d:
            annuncio["descrizione"] = pulisci(d.get_text(" "))[:3000]


def fonte_adzuna(parola, comune):
    url = f"https://www.adzuna.it/search?q={quote_plus(parola)}&w={quote_plus(comune)}&sb=date"
    pagina = scarica(url)
    if not pagina:
        return []
    s = BeautifulSoup(pagina, "html.parser")
    risultati = []
    for art in s.find_all("article"):
        a = art.select_one("h2 a")
        if not a or "/details/" not in a.get("href", ""):
            continue
        id_ = re.search(r"/details/(\d+)", a["href"])
        azienda = art.select_one(".ui-company")
        luogo = art.select_one(".ui-location")
        snippet = art.select_one(".max-snippet-height")
        risultati.append(nuovo_annuncio(
            "Adzuna", id_.group(1) if id_ else a["href"], a.get_text(),
            azienda.get_text() if azienda else "", luogo.get_text() if luogo else "",
            a["href"].split("?")[0], snippet.get_text() if snippet else ""))
    return risultati


def fonte_talent(parola, comune):
    url = f"https://it.talent.com/jobs?k={quote_plus(parola)}&l={quote_plus(comune)}"
    pagina = scarica(url)
    if not pagina:
        return []
    s = BeautifulSoup(pagina, "html.parser")
    risultati = []
    for card in s.find_all("article", class_=re.compile(r"JobCard_card")):
        titolo = card.find(class_=re.compile(r"JobCard_title"))
        link = card.find("a", href=re.compile(r"/view\?id="))
        if not (titolo and link):
            continue
        id_ = re.search(r"id=(\d+)", link["href"])
        azienda = card.find(class_=re.compile(r"JobCard_company"))
        luogo = card.find(class_=re.compile(r"JobCard_location"))
        snippet = card.find(class_=re.compile(r"JobCard_snippet"))
        data = card.find("time")
        risultati.append(nuovo_annuncio(
            "Talent.com", id_.group(1) if id_ else link["href"], titolo.get_text(),
            azienda.get_text() if azienda else "", luogo.get_text() if luogo else "",
            urljoin("https://it.talent.com", link["href"]),
            snippet.get_text(" ") if snippet else "",
            data.get("datetime", "") if data else ""))
    return risultati


def fonte_jobrapido(parola, comune):
    # Jobrapido raccoglie anche annunci di Indeed, Subito, InfoJobs, agenzie, ecc.
    url = f"https://it.jobrapido.com/?w={quote_plus(parola)}&l={quote_plus(comune)}&r=auto&shm=all"
    pagina = scarica(url)
    if not pagina:
        return []
    decoder = json.JSONDecoder()
    trovati = {}
    # gli annunci sono oggetti JSON dentro uno script: risalgo dalla chiave "advertId"
    # fino alla graffa che apre l'oggetto dell'annuncio
    for m in re.finditer(r'"advertId":"(\d+)"', pagina):
        if m.group(1) in trovati:
            continue
        pos = m.start()
        for _ in range(15):
            pos = pagina.rfind("{", 0, pos)
            if pos < 0:
                break
            try:
                o, _fine = decoder.raw_decode(pagina, pos)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("advertId") == m.group(1) and o.get("openAdvertUrl"):
                trovati[m.group(1)] = o
                break
    risultati = []
    for id_, sr in trovati.items():
        titolo = pulisci(sr.get("title", ""))
        link = sr.get("openAdvertUrl", "")
        if not titolo:
            continue
        risultati.append(nuovo_annuncio(
            "Jobrapido", id_, titolo, sr.get("company", ""),
            sr.get("location", "") or ", ".join(sr.get("locations") or []),
            link, sr.get("bodyHighlighted") or "", sr.get("date") or ""))
    return risultati


def fonte_randstad(parola, comune):
    url = f"https://www.randstad.it/offerte-lavoro/q-{quote_plus(parola)}/{normalizza(comune)}/"
    pagina = scarica(url)
    if not pagina:
        return []
    s = BeautifulSoup(pagina, "html.parser")
    risultati = []
    visti = set()
    for a in s.find_all("a", href=re.compile(r"/offerte-lavoro/[^/]+_[0-9a-f-]{36}/?$")):
        href = a["href"]
        if href in visti:
            continue
        visti.add(href)
        card = a
        for _ in range(6):  # risali fino al riquadro dell'annuncio
            if card.parent is None:
                break
            card = card.parent
        testi = [t for t in card.stripped_strings]
        titolo = a.get_text(" ", strip=True)
        luogo = ""
        if titolo in testi:
            i = testi.index(titolo)
            if i + 1 < len(testi):
                luogo = testi[i + 1]
        id_ = href.rstrip("/").rsplit("_", 1)[-1]
        risultati.append(nuovo_annuncio(
            "Randstad", id_, titolo, "Randstad", luogo,
            urljoin("https://www.randstad.it", href), " ".join(testi)))
    return risultati


def dettaglio_talent(annuncio):
    pagina = scarica(annuncio["url"])
    if pagina:
        d = BeautifulSoup(pagina, "html.parser").find(class_=re.compile(r"(?i)description"))
        if d:
            annuncio["descrizione"] = pulisci(d.get_text(" "))[:3000]


def dettaglio_adzuna(annuncio):
    pagina = scarica(annuncio["url"])
    if pagina:
        d = BeautifulSoup(pagina, "html.parser").select_one("section.adp-body")
        if d:
            annuncio["descrizione"] = pulisci(d.get_text(" "))[:3000]


FONTI = [fonte_linkedin, fonte_jobrapido, fonte_talent, fonte_adzuna, fonte_randstad]
DETTAGLI = {"LinkedIn": dettaglio_linkedin, "Talent.com": dettaglio_talent, "Adzuna": dettaglio_adzuna}
MAX_DETTAGLI = {"LinkedIn": 10, "Talent.com": 25, "Adzuna": 25}  # per ciclo, per non essere bloccati
FONTI_BLOCCATE_NEL_CLOUD = {fonte_adzuna}  # Adzuna rifiuta i server di GitHub (HTTP 405)


# ----------------------------------------------------------------------------- filtri

class Filtri:
    def __init__(self, cfg):
        self.comuni = {
            nome: re.compile(r"\b(" + "|".join(map(re.escape, alias)) + r")\b")
            for nome, alias in cfg["comuni"].items()
        }
        self.lavori = {
            nome: (re.compile(d["regex"]), d.get("cerca_anche_in_azienda", False),
                   re.compile(d["azienda_regex"]) if d.get("azienda_regex") else None,
                   re.compile(d["oppure_azienda_regex"]) if d.get("oppure_azienda_regex") else None)
            for nome, d in cfg["lavori"].items()
        }
        self.senza_filtri_profilo = {n for n, d in cfg["lavori"].items() if d.get("salta_filtri_profilo")}
        self.escludi = [normalizza(x) for x in cfg.get("escludi_se_titolo_contiene", [])]
        self.senza_esp = re.compile("|".join(normalizza(f) for f in cfg["frasi_senza_esperienza"]))
        sp = cfg.get("spostamenti", {})
        self.auto = self._rx(sp.get("escludi_se_richiede_auto"))
        self.serale = self._rx(sp.get("orari_serali_notturni"))
        self.serali_ok = set(sp.get("serali_ok_solo_a", []))
        self.dubbi = self._rx(cfg.get("escludi_se_testo_contiene"))
        self.programmazione = self._rx(cfg.get("escludi_programmazione"))
        self.settori = self._rx(cfg.get("settori_non_adatti"))
        self.eccezioni = self._rx(cfg.get("eccezioni_settori"))
        adatti = cfg.get("lavori_adatti", {})
        self.adatti = self._rx(adatti.get("campo_di_studi", []) + adatti.get("per_tutti", []))
        self.requisiti = self._rx(cfg.get("requisiti_mancanti"))
        self.con_settori = {n for n, d in cfg["lavori"].items() if d.get("escludi_settori_non_adatti")}

    def comune(self, a):
        luogo = normalizza(a["luogo"])
        testo = luogo or normalizza(a["titolo"] + " " + a["descrizione"])
        testo = re.sub(r"\brivalta di torino\b", "rivalta", testo)  # comune a sé, non Torino città
        for nome, rx in self.comuni.items():
            if rx.search(testo):
                return nome
        return None

    def lavoro(self, a):
        titolo = normalizza(a["titolo"])
        if any(x in titolo for x in self.escludi):
            return None
        azienda = normalizza(a["azienda"])
        for nome, (rx, anche_azienda, rx_azienda, rx_oppure) in self.lavori.items():
            if rx_azienda and not (rx_azienda.search(azienda) or rx_azienda.search(titolo)):
                continue  # categoria legata a un'azienda precisa
            if rx.search(titolo) or (anche_azienda and rx.search(azienda)):
                return nome
            # catena della lista + ruolo che può fare chiunque (no manager, uffici...)
            if rx_oppure and rx_oppure.search(azienda) and self.adatti and self.adatti.search(titolo):
                return nome
        return None

    @staticmethod
    def _rx(frasi):
        frasi = [normalizza(f) for f in (frasi or []) if f]
        return re.compile("|".join(frasi)) if frasi else None

    def senza_esperienza(self, a):
        return bool(self.senza_esp.search(normalizza(a["titolo"] + " " + a["descrizione"])))

    def motivo_esclusione(self, a):
        testo = normalizza(a["titolo"] + " " + a["descrizione"])
        if self.dubbi and self.dubbi.search(testo + " " + normalizza(a.get("azienda", ""))):
            return "offerta dubbia"
        if self.auto and self.auto.search(testo):
            return "richiede auto"
        if self.serale and a.get("comune") not in self.serali_ok and self.serale.search(testo):
            return "orario serale/notturno"
        titolo = normalizza(a["titolo"])
        if a.get("lavoro") in self.senza_filtri_profilo:
            return ""
        if self.programmazione and self.programmazione.search(titolo):
            return "programmazione"
        if (self.settori and a.get("lavoro") in self.con_settori and self.settori.search(titolo)
                and not (self.eccezioni and self.eccezioni.search(titolo))):
            return "settore non adatto"
        if self.adatti and a.get("lavoro") in self.con_settori and not self.adatti.search(titolo):
            return "fuori dal tuo campo"
        if self.requisiti and self.requisiti.search(testo):
            return "requisiti mancanti"
        return ""


def impronta(a):
    """Serve a riconoscere lo stesso annuncio pubblicato su più portali."""
    t = re.sub(r"[^a-z0-9 ]", " ", normalizza(a["titolo"]))
    t = re.sub(r"\b(m|f|mf|m f|nb|m f nb|a|e|o)\b", " ", t)
    az = normalizza(a["azienda"])
    az = re.sub(r"\b(s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?n\.?c\.?|s\.?a\.?s\.?|group|italia|filiale.*)\b", " ", az)
    az = re.sub(r"[^a-z0-9]", "", az)[:12]
    return f"{' '.join(t.split())}|{az}|{a.get('comune', '')}"


# ----------------------------------------------------------------------------- notifiche

def notifica_windows(titolo, testo, url):
    try:
        from winotify import Notification, audio
        n = Notification(app_id="Cerca Lavoro", title=titolo[:120], msg=testo[:250],
                         launch=url or FILE_PAGINA.as_uri())
        n.set_audio(audio.Default, loop=False)
        if url:
            n.add_actions(label="Apri annuncio", launch=url)
        n.show()
    except Exception as e:
        log.warning("Notifica Windows non riuscita: %s", e)


def notifica_telefono(cfg, titolo, testo, url, urgente=False):
    n = cfg["notifiche"]
    try:
        r = requests.post(n["ntfy_server"].rstrip("/"), timeout=15, json={
            "topic": n["ntfy_topic"],
            "title": titolo,
            "message": testo,
            "click": url,
            "priority": 5 if urgente else 3,
            "tags": ["star"] if urgente else ["briefcase"],
        })
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Notifica telefono non riuscita: %s", e)


def invia(cfg, a):
    stella = "⭐ SENZA ESPERIENZA - " if a["senza_esperienza"] else ""
    titolo = f"{stella}{a['lavoro']} a {a['comune']}"
    testo = f"{a['titolo']}"
    if a["azienda"]:
        testo += f"\n{a['azienda']}"
    testo += f"\n{a['luogo'] or a['comune']}  ·  {a['fonte']}"
    if cfg["notifiche"].get("windows"):
        notifica_windows(titolo, testo, a["url"])
    if cfg["notifiche"].get("telefono_ntfy") and cfg["notifiche"].get("ntfy_topic"):
        notifica_telefono(cfg, titolo, testo, a["url"], a["senza_esperienza"])


def invia_riepilogo(cfg, quanti, quanti_senza):
    titolo = f"Altri {quanti} nuovi annunci"
    dove = "tocca per aprire la lista completa" if PAGINA_URL else "apri la lista completa sul PC (annunci.html)"
    testo = f"Di cui {quanti_senza} senza esperienza: {dove}."
    if cfg["notifiche"].get("windows"):
        notifica_windows(titolo, testo, FILE_PAGINA.as_uri())
    if cfg["notifiche"].get("telefono_ntfy") and cfg["notifiche"].get("ntfy_topic"):
        notifica_telefono(cfg, titolo, testo, PAGINA_URL, False)


# ----------------------------------------------------------------------------- dati

def carica_config():
    """config.json (pubblico) + privato.json (dati personali, solo sul PC)
    + variabili d'ambiente (segreti di GitHub quando gira nel cloud)."""
    cfg = json.loads(FILE_CONFIG.read_text(encoding="utf-8"))
    fonti_private = []
    if FILE_PRIVATO.exists():
        fonti_private.append(FILE_PRIVATO.read_text(encoding="utf-8"))
    if os.environ.get("PRIVATO_JSON"):  # segreto di GitHub con il contenuto di privato.json
        fonti_private.append(os.environ["PRIVATO_JSON"])
    for testo in fonti_private:
        privato = json.loads(testo)
        for sezione, valori in privato.items():
            if isinstance(valori, dict):
                cfg.setdefault(sezione, {}).update(valori)
            else:
                cfg[sezione] = valori

    env = os.environ
    cand = cfg.setdefault("candidature", {})
    for var, sezione, chiave in [
        ("NTFY_TOPIC", "notifiche", "ntfy_topic"),
        ("CAND_NOME", "candidature", "nome"),
        ("CAND_TELEFONO", "candidature", "telefono"),
        ("CAND_EMAIL", "candidature", "email_mittente"),
        ("SMTP_PASSWORD_APP", "candidature", "smtp_password_app"),
        ("CV_PATH", "candidature", "cv"),
    ]:
        if env.get(var):
            cfg[sezione][chiave] = env[var]
    if env.get("CANDIDATURE_ATTIVE"):
        cand["attivo"] = env["CANDIDATURE_ATTIVE"].strip().lower() in ("1", "true", "si", "sì")

    if sys.platform != "win32":
        cfg["notifiche"]["windows"] = False

    if not cfg["notifiche"].get("ntfy_topic") and not NEL_CLOUD:
        # nome canale casuale e difficile da indovinare: chi lo conosce riceve le notifiche
        topic = "lavoro-to-" + secrets.token_hex(6)
        cfg["notifiche"]["ntfy_topic"] = topic
        privato = json.loads(FILE_PRIVATO.read_text(encoding="utf-8")) if FILE_PRIVATO.exists() else {}
        privato.setdefault("notifiche", {})["ntfy_topic"] = topic
        FILE_PRIVATO.write_text(json.dumps(privato, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def carica_dati():
    if FILE_DATI.exists():
        try:
            return json.loads(FILE_DATI.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("annunci_visti.json danneggiato: riparto da zero")
    return {"annunci": {}, "impronte": [], "primo_avvio_fatto": False}


def salva_dati(dati):
    limite = (datetime.now() - timedelta(days=GIORNI_DA_TENERE)).isoformat()
    dati["annunci"] = {k: v for k, v in dati["annunci"].items() if v["trovato_il"] >= limite}
    dati["impronte"] = sorted({a["impronta"] for a in dati["annunci"].values()})
    tmp = FILE_DATI.with_suffix(".tmp")
    tmp.write_text(json.dumps(dati, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE_DATI)


# ----------------------------------------------------------------------------- candidature

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_DA_IGNORARE = re.compile(
    r"noreply|no-reply|donotreply|privacy|dpo@|gdpr|unsubscribe|example|"
    r"@(linkedin|jobrapido|talent|adzuna|indeed|subito|infojobs|sentry|wixpress)\.", re.I)


def visibile(a):
    return not a.get("doppione") and not a.get("nascosto") and not a.get("escluso")


def trova_email(a):
    for e in EMAIL_RX.findall(a.get("descrizione", "")):
        e = e.strip(".").lower()
        if not EMAIL_DA_IGNORARE.search(e):
            return e
    return ""


def scrivi_lettera(cfg, a):
    c = cfg["candidature"]
    campi = {
        "titolo": a["titolo"],
        "azienda": a["azienda"] or "",
        "destinatario": a["azienda"] or "Responsabile della selezione",
        "frase": c.get("frasi_per_lavoro", {}).get(a["lavoro"], ""),
        "nome": c.get("nome", ""),
        "telefono": c.get("telefono", ""),
        "email": c.get("email_mittente", ""),
    }
    oggetto = c["oggetto"].format(**campi)
    testo = re.sub(r"\n{3,}", "\n\n", c["lettera"].format(**campi))
    return oggetto, testo


def manda_email(cfg, destinatario, oggetto, testo):
    import smtplib
    from email.message import EmailMessage
    c = cfg["candidature"]
    cv = Path(c["cv"])
    msg = EmailMessage()
    msg["From"] = f'{c.get("nome", "")} <{c["email_mittente"]}>'
    msg["To"] = destinatario
    msg["Subject"] = oggetto
    msg.set_content(testo)
    msg.add_attachment(cv.read_bytes(), maintype="application", subtype="pdf",
                       filename=f"CV {c.get('nome', '')}.pdf".strip())
    with smtplib.SMTP_SSL(c["smtp_server"], int(c.get("smtp_porta", 465)), timeout=30) as s:
        s.login(c["email_mittente"], c["smtp_password_app"].replace(" ", ""))
        s.send_message(msg)


def candidature_pronte(cfg):
    c = cfg.get("candidature", {})
    if not c.get("attivo"):
        return False
    if not c.get("smtp_password_app") or not c.get("email_mittente"):
        log.warning("Candidature attive ma manca email o password per le app: nessun invio")
        return False
    if not Path(c.get("cv", "")).is_file():
        log.warning("CV non trovato in %s: nessun invio", c.get("cv"))
        return False
    return True


def candidature_automatiche(cfg, dati, nuovi):
    if not candidature_pronte(cfg):
        return
    c = cfg["candidature"]
    storico = dati.setdefault("candidature", [])
    oggi = datetime.now().date().isoformat()
    inviate_oggi = sum(1 for x in storico if x["data"].startswith(oggi))
    limite = (datetime.now() - timedelta(days=30)).isoformat()
    email_recenti = {x["email"] for x in storico if x["data"] >= limite}

    for a in nuovi:
        if inviate_oggi >= c.get("max_al_giorno", 15):
            log.info("Raggiunto il massimo di candidature per oggi")
            break
        if c.get("solo_senza_esperienza") and not a["senza_esperienza"]:
            continue
        email = trova_email(a)
        if not email or email in email_recenti:
            continue
        oggetto, testo = scrivi_lettera(cfg, a)
        try:
            manda_email(cfg, email, oggetto, testo)
        except Exception as e:
            log.error("Invio CV a %s non riuscito: %s", email, e)
            continue
        inviate_oggi += 1
        email_recenti.add(email)
        a["candidato_il"] = datetime.now().isoformat(timespec="seconds")
        storico.append({"data": a["candidato_il"], "email": email, "id": a["id"],
                        "titolo": a["titolo"], "azienda": a["azienda"]})
        log.info("CV inviato a %s per %s", email, a["titolo"])
        titolo = f"📨 CV inviato: {a['lavoro']} a {a['comune']}"
        corpo = f"{a['titolo']}\n{a['azienda']}\nInviato a {email}"
        if cfg["notifiche"].get("windows"):
            notifica_windows(titolo, corpo, a["url"])
        if cfg["notifiche"].get("telefono_ntfy"):
            notifica_telefono(cfg, titolo, corpo, a["url"], False)
        time.sleep(5)


# ----------------------------------------------------------------------------- controllo

def controlla(cfg, notifica=True):
    filtri = Filtri(cfg)
    dati = carica_dati()
    primo_avvio = not dati.get("primo_avvio_fatto")
    impronte = set(dati.get("impronte", []))
    nuovi = []
    errori_fonti = 0

    for nome_lavoro, lavoro in cfg["lavori"].items():
        # per le categorie generiche basta cercare su Torino: il filtro sul luogo
        # tiene comunque anche Orbassano, Beinasco e Piossasco se compaiono
        comuni_ricerca = ["Torino"] if lavoro.get("solo_torino") else list(cfg["comuni"])
        for parola in lavoro["ricerche"]:
            for comune in comuni_ricerca:
                for fonte in FONTI:
                    if NEL_CLOUD and fonte in FONTI_BLOCCATE_NEL_CLOUD:
                        continue
                    try:
                        trovati = fonte(parola, comune)
                    except Exception as e:  # un portale che cambia layout non deve fermare gli altri
                        errori_fonti += 1
                        log.exception("Errore in %s (%s, %s): %s", fonte.__name__, parola, comune, e)
                        continue
                    for a in trovati:
                        if a["id"] in dati["annunci"]:
                            continue
                        a["comune"] = filtri.comune(a)
                        a["lavoro"] = filtri.lavoro(a)
                        if not a["comune"] or not a["lavoro"]:
                            continue
                        a["impronta"] = impronta(a)
                        a["trovato_il"] = datetime.now().isoformat(timespec="seconds")
                        doppione = a["impronta"] in impronte
                        a["doppione"] = doppione
                        dati["annunci"][a["id"]] = a
                        impronte.add(a["impronta"])
                        if not doppione:
                            nuovi.append(a)

    # descrizione completa: serve per capire se chiedono esperienza, auto, orari serali
    # e per trovare l'email a cui mandare il CV (solo per gli annunci nuovi)
    BLOCCATI.clear()
    letti = {}
    for a in nuovi:
        fonte = a["fonte"]
        if fonte not in DETTAGLI or letti.get(fonte, 0) >= MAX_DETTAGLI[fonte]:
            continue
        if any(dom in a["url"] for dom in BLOCCATI):
            continue
        serve_sempre = cfg["lavori"].get(a["lavoro"], {}).get("solo_senza_esperienza")
        if primo_avvio and not serve_sempre:
            continue
        letti[fonte] = letti.get(fonte, 0) + 1
        try:
            DETTAGLI[fonte](a)
        except Exception as e:
            log.warning("Dettaglio non letto per %s: %s", a["url"], e)

    for a in dati["annunci"].values():
        a["senza_esperienza"] = filtri.senza_esperienza(a)
        a["escluso"] = filtri.motivo_esclusione(a)

    # categorie in cui interessano solo gli annunci senza esperienza
    for a in nuovi:
        regole = cfg["lavori"].get(a["lavoro"], {})
        if regole.get("solo_senza_esperienza") and not a["senza_esperienza"]:
            a["nascosto"] = True
    scartati = [a for a in nuovi if a["escluso"] and not a.get("nascosto")]
    for a in scartati:
        log.info("Scartato (%s): %s - %s", a["escluso"], a["titolo"], a["comune"])
    nuovi = [a for a in nuovi if not a.get("nascosto") and not a["escluso"]]

    nuovi.sort(key=lambda a: not a["senza_esperienza"])  # prima quelli senza esperienza
    log.info("Controllo finito: %d annunci nuovi (%d senza esperienza)%s",
             len(nuovi), sum(a["senza_esperienza"] for a in nuovi),
             " - primo avvio, nessuna notifica" if primo_avvio else "")

    if notifica and not primo_avvio and nuovi:
        massimo = 12
        for a in nuovi[:massimo]:
            invia(cfg, a)
            time.sleep(1)
        if len(nuovi) > massimo:
            resto = nuovi[massimo:]
            invia_riepilogo(cfg, len(resto), sum(a["senza_esperienza"] for a in resto))
    elif notifica and primo_avvio:
        n = len([a for a in dati["annunci"].values() if visibile(a)])
        msg = (f"Ho trovato {n} annunci già pubblicati (li vedi in annunci.html). "
               "Da adesso ti avviso solo per quelli nuovi.")
        if cfg["notifiche"].get("windows"):
            notifica_windows("Cerca Lavoro è attivo ✅", msg, FILE_PAGINA.as_uri())
        if cfg["notifiche"].get("telefono_ntfy"):
            notifica_telefono(cfg, "Cerca Lavoro è attivo ✅", msg, "", False)

    if not primo_avvio:
        candidature_automatiche(cfg, dati, nuovi)

    dati["primo_avvio_fatto"] = True
    dati["ultimo_controllo"] = datetime.now().isoformat(timespec="seconds")
    salva_dati(dati)
    crea_pagina(dati, cfg)
    return nuovi


# ----------------------------------------------------------------------------- pagina html

def crea_pagina(dati, cfg):
    if NEL_CLOUD:
        # la pagina online è pubblica: niente nome, telefono o email nella lettera
        cfg = json.loads(json.dumps(cfg))
        cfg["candidature"].update(nome="[Nome e Cognome]", telefono="[telefono]", email_mittente="[email]")
    annunci = [a for a in dati["annunci"].values() if visibile(a)]
    annunci.sort(key=lambda a: (a["senza_esperienza"], a["trovato_il"]), reverse=True)
    categorie = list(cfg["lavori"].keys())

    def riga(a):
        stella = '<span class="tag star">⭐ Senza esperienza</span>' if a["senza_esperienza"] else ""
        quando = datetime.fromisoformat(a["trovato_il"]).strftime("%d/%m %H:%M")
        desc = html.escape(a["descrizione"][:300]) + ("…" if len(a["descrizione"]) > 300 else "")
        inviato = ""
        if a.get("candidato_il"):
            q = datetime.fromisoformat(a["candidato_il"]).strftime("%d/%m %H:%M")
            inviato = f'<span class="tag sent">📨 CV inviato il {q}</span>'
        oggetto, lettera = scrivi_lettera(cfg, a) if "candidature" in cfg else ("", "")
        email = trova_email(a)
        mail = ""
        if email and not a.get("candidato_il"):
            mail = (f'<a class="btn" href="mailto:{html.escape(email)}?subject={quote_plus(oggetto).replace("+", "%20")}'
                    f'&body={quote_plus(lettera).replace("+", "%20")}">✉️ Email a {html.escape(email)}</a>')
        return f"""
<article data-cat="{html.escape(a['lavoro'])}" data-esp="{int(a['senza_esperienza'])}">
  <div class="top">{stella}{inviato}<span class="tag">{html.escape(a['lavoro'])}</span>
    <span class="tag loc">📍 {html.escape(a['comune'])}</span></div>
  <h2><a href="{html.escape(a['url'])}" target="_blank" rel="noopener">{html.escape(a['titolo'])}</a></h2>
  <p class="meta">{html.escape(a['azienda'] or '—')} · {html.escape(a['luogo'])} · {html.escape(a['fonte'])} · trovato il {quando}</p>
  <p class="desc">{desc}</p>
  <div class="azioni">
    <a class="btn primary" href="{html.escape(a['url'])}" target="_blank" rel="noopener">Candidati sul sito</a>
    <button class="btn copia" data-lettera="{html.escape(lettera)}">📋 Copia lettera</button>{mail}
  </div>
</article>"""

    bottoni = "".join(f'<button data-f="{html.escape(c)}">{html.escape(c)}</button>' for c in categorie)
    ultimo = datetime.fromisoformat(dati["ultimo_controllo"]).strftime("%d/%m/%Y %H:%M")
    pagina = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Annunci di lavoro</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --text:#1d2330; --muted:#667085; --accent:#2563eb; --star:#fff4cc; --starb:#e0b000; --line:#e4e7ec; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#11141a; --card:#1a1f29; --text:#e6e9ef; --muted:#98a2b3; --accent:#6ea2ff; --star:#3a3112; --starb:#d7a800; --line:#2a3140; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif; }}
header {{ position:sticky; top:0; background:var(--bg); padding:16px; border-bottom:1px solid var(--line); z-index:1; }}
h1 {{ margin:0 0 4px; font-size:20px; }}
.sub {{ color:var(--muted); font-size:13px; margin:0 0 10px; }}
.filtri {{ display:flex; flex-wrap:wrap; gap:6px; }}
.filtri button {{ border:1px solid var(--line); background:var(--card); color:var(--text); border-radius:999px; padding:5px 12px; cursor:pointer; font:inherit; font-size:13px; }}
.filtri button.on {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
main {{ max-width:820px; margin:0 auto; padding:12px 16px 40px; }}
article {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin:10px 0; }}
article[data-esp="1"] {{ border-left:4px solid var(--starb); }}
h2 {{ font-size:16px; margin:6px 0 2px; }}
h2 a {{ color:var(--accent); text-decoration:none; }}
.meta {{ color:var(--muted); font-size:13px; margin:0; }}
.desc {{ font-size:13px; margin:6px 0 0; }}
.tag {{ display:inline-block; font-size:12px; padding:2px 8px; border-radius:6px; background:var(--bg); margin-right:4px; }}
.tag.star {{ background:var(--star); font-weight:600; }}
.vuoto {{ color:var(--muted); text-align:center; padding:40px; }}
.tag.sent {{ background:#d9f7e3; color:#11602f; font-weight:600; }}
@media (prefers-color-scheme: dark) {{ .tag.sent {{ background:#123d24; color:#a6eebf; }} }}
.azioni {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
.btn {{ border:1px solid var(--line); background:var(--bg); color:var(--text); border-radius:8px; padding:6px 12px; font:inherit; font-size:13px; cursor:pointer; text-decoration:none; }}
.btn.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
</style></head><body>
<header>
  <h1>Annunci di lavoro</h1>
  <p class="sub">{len(annunci)} annunci · {html.escape(', '.join(cfg['comuni']))} · ultimo controllo {ultimo}</p>
  <div class="filtri"><button data-f="" class="on">Tutti</button><button data-f="__esp">⭐ Senza esperienza</button>{bottoni}</div>
</header>
<main>{''.join(riga(a) for a in annunci) or '<p class="vuoto">Nessun annuncio ancora.</p>'}</main>
<script>
document.querySelectorAll('.filtri button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.filtri button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const f = b.dataset.f;
  document.querySelectorAll('article').forEach(a => {{
    a.style.display = (!f || (f === '__esp' ? a.dataset.esp === '1' : a.dataset.cat === f)) ? '' : 'none';
  }});
}});
document.querySelectorAll('.copia').forEach(b => b.onclick = () => {{
  const t = document.createElement('textarea');
  t.value = b.dataset.lettera; document.body.appendChild(t); t.select();
  document.execCommand('copy'); t.remove();
  const prima = b.textContent; b.textContent = '✅ Copiata'; setTimeout(() => b.textContent = prima, 1500);
}});
</script>
</body></html>"""
    FILE_PAGINA.write_text(pagina, encoding="utf-8")


# ----------------------------------------------------------------------------- avvio

def configura_log():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(FILE_LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def main():
    configura_log()
    cfg = carica_config()
    arg = sys.argv[1:]

    if "--test" in arg:
        notifica_windows("Prova Cerca Lavoro", "Se vedi questo messaggio le notifiche sul PC funzionano.", "")
        notifica_telefono(cfg, "Prova Cerca Lavoro",
                          "Se vedi questo messaggio le notifiche sul telefono funzionano.", "", True)
        print("Notifica di prova inviata. Canale ntfy:", cfg["notifiche"]["ntfy_topic"])
        return

    if "--prova-email" in arg:
        # manda il CV a te stesso per controllare che l'invio funzioni
        c = cfg["candidature"]
        if not c.get("smtp_password_app"):
            print("Manca 'smtp_password_app' in config.json (vedi LEGGIMI.txt).")
            return
        prova = {"titolo": "Annuncio di prova", "azienda": "", "lavoro": "Cameriere"}
        oggetto, testo = scrivi_lettera(cfg, prova)
        manda_email(cfg, c["email_mittente"], "[PROVA] " + oggetto, testo)
        print("Email di prova inviata a", c["email_mittente"])
        return

    if "--una-volta" in arg:
        controlla(cfg)
        if "--apri" in arg:
            webbrowser.open(FILE_PAGINA.as_uri())
        return

    # una sola copia alla volta
    blocco = socket.socket()
    try:
        blocco.bind(("127.0.0.1", 47613))
    except OSError:
        log.info("Cerca Lavoro è già in esecuzione.")
        return

    log.info("Cerca Lavoro avviato (ogni %s minuti)", cfg["intervallo_minuti"])
    while True:
        inizio = time.time()
        try:
            cfg = carica_config()
            controlla(cfg)
        except Exception as e:
            log.exception("Errore durante il controllo: %s", e)
        attesa = max(5, cfg.get("intervallo_minuti", 10)) * 60 - (time.time() - inizio)
        time.sleep(max(60, attesa))


if __name__ == "__main__":
    main()
