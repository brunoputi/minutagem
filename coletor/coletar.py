"""
Coletor de minutagem (fonte: API pública do ESPN) — roda no GitHub Actions.

Gera:
  docs/data/dados.json   -> dados que o dashboard lê
  posicoes.csv           -> grupos de posição (sua coluna grupo_manual é preservada)
  cache/<liga>/          -> calendário e jogos já processados (só baixa o que é novo)
  cache/auditoria.txt    -> jogos cuja soma de minutos não fecha

Para incluir outra competição, descomente a linha dela em LIGAS.
"""
import csv
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

LIGAS = {
    "bra.1": "Brasileirão",
    # "conmebol.libertadores": "Libertadores",
    # "conmebol.sudamericana": "Sul-Americana",
    # "bra.copa_do_brazil": "Copa do Brasil",
}
ANO = date.today().year
DURACAO = 90
DIAS_PARA_FIXAR = 2  # jogos mais novos que isso são reprocessados a cada execução

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
SAIDA = ROOT / "docs" / "data"
POSICOES = ROOT / "posicoes.csv"
HEADERS = {"User-Agent": "Mozilla/5.0"}

ORDEM_GRUPOS = ["GOL", "LAT", "ZAG", "VOL", "MEI", "EXT", "ATA"]
MAPA = {
    "GOL": {"G", "GK"},
    "ZAG": {"CD", "CD-L", "CD-R", "CB", "SW"},
    "LAT": {"LB", "RB", "LWB", "RWB", "WB"},
    "VOL": {"DM", "DM-L", "DM-R", "CM", "CM-L", "CM-R"},
    "MEI": {"AM", "AM-L", "AM-R", "M"},
    "EXT": {"LM", "RM", "LW", "RW"},
    "ATA": {"F", "FW", "CF", "CF-L", "CF-R", "ST", "S"},
}


def grupo_de(pos):
    for g, conj in MAPA.items():
        if pos in conj:
            return g
    return "—"


def get(url, **params):
    for tentativa in range(3):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (tentativa + 1))
    return None


def norm(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt).strip().lower()


def parse_clock(s):
    m = re.match(r"\s*(\d+)'?\s*(?:\+\s*(\d+))?", s or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


# ------------------------------------------------------------------ calendário
def listar_eventos(liga):
    base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}"
    pasta = CACHE / liga
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / "eventos.json"
    eventos, inicio = {}, date(ANO, 1, 1)
    if arq.exists():
        dados = json.loads(arq.read_text(encoding="utf-8"))
        if dados.get("ano") == ANO:
            eventos = dados["eventos"]
            inicio = max(inicio, date.fromisoformat(dados["ate"]) - timedelta(days=10))
    fim = min(date.today(), date(ANO, 12, 31))
    print(f"[{liga}] calendário {inicio:%d/%m} a {fim:%d/%m}")
    d = inicio
    while d <= fim:
        sb = get(f"{base}/scoreboard", dates=d.strftime("%Y%m%d"))
        for e in (sb or {}).get("events", []):
            comp = e["competitions"][0]
            lado = {c["homeAway"]: c for c in comp["competitors"]}
            eventos[e["id"]] = {
                "id": e["id"], "data": e["date"][:10],
                "casa_id": str(lado.get("home", {}).get("team", {}).get("id")),
                "fora_id": str(lado.get("away", {}).get("team", {}).get("id")),
                "gols_casa": lado.get("home", {}).get("score"),
                "gols_fora": lado.get("away", {}).get("score"),
                "encerrado": e["status"]["type"]["completed"],
            }
        d += timedelta(days=1)
        time.sleep(0.15)
    arq.write_text(json.dumps({"ano": ANO, "ate": fim.isoformat(), "eventos": eventos},
                              ensure_ascii=False), encoding="utf-8")
    return base, eventos


# ------------------------------------------------------------------ tempo real
def duracao_tempos(det):
    itens = []
    for c in det.get("commentary", []) or []:
        itens.append(((c.get("time") or {}).get("displayValue", ""), c.get("text", "")))
    for k in det.get("keyEvents", []) or []:
        t = k.get("type") or {}
        itens.append(((k.get("clock") or {}).get("displayValue", ""),
                      f"{t.get('type', '')} {t.get('text', '')} {k.get('text', '')}"))
    h1 = h2 = None
    for clk, txt in itens:
        tx, pc = norm(txt), parse_clock(clk)
        if not pc:
            continue
        if re.search(r"first half ends|halftime|half-time|end of (the )?first half", tx) and pc[0] >= 45:
            h1 = max(h1 or 0, pc[0] + pc[1])
        elif re.search(r"second half ends|match ends|full[- ]?time|end regular time", tx) and pc[0] >= 90:
            h2 = max(h2 or 0, pc[0] + pc[1] - 45)
    return h1, h2


def tempo_real(base, add, h1, total):
    if base < 45:
        t = base
    elif base == 45 and add > 0:
        t = 45 + add
    elif base == 45:
        t = h1
    else:
        t = h1 + (base - 45) + add
    return min(t, total)


# ------------------------------------------------------------------ um jogo
def processar_jogo(ev, det):
    jogadores, nomes, lados = {}, defaultdict(dict), {}
    for bloco in det.get("rosters", []):
        team = bloco.get("team", {})
        t = team.get("displayName", "?")
        lado = bloco.get("homeAway") or ("home" if str(team.get("id")) == ev["casa_id"] else "away")
        lados[lado] = t
        for p in bloco.get("roster", []):
            ath = p.get("athlete", {})
            aid = str(ath.get("id"))
            jogadores[aid] = {
                "time": t, "nome": ath.get("displayName", "?"),
                "pos": (p.get("position") or {}).get("abbreviation", ""),
                "starter": bool(p.get("starter")), "subbedIn": bool(p.get("subbedIn")),
            }
            for n in (ath.get("displayName"), ath.get("shortName"), ath.get("fullName")):
                if n:
                    nomes[t][norm(n)] = aid
    times = list(nomes.keys())

    def achar(nome, t_txt):
        n = re.split(r" because| due to| \(", norm(nome))[0].strip(" .")
        if not n:
            return None
        tt = norm(t_txt)
        alvo = [t for t in times if tt and (tt in norm(t) or norm(t) in tt)] or times
        for t in alvo:
            d = nomes[t]
            if n in d:
                return d[n]
            toks = n.split()
            c = {a for k, a in d.items() if k.split() and k.split()[-1] == toks[-1]}
            if len(c) == 1:
                return c.pop()
        return None

    h1, h2 = duracao_tempos(det)
    total = (h1 + h2) if (h1 and h2) else None
    em_campo = {a for a, j in jogadores.items() if j["starter"]}
    entrada, saida, expulso, nao_casadas = {}, {}, {}, []

    for k in det.get("keyEvents", []) or []:
        tobj = k.get("type") or {}
        tipo = norm(f"{tobj.get('type', '')} {tobj.get('text', '')}")
        texto = k.get("text", "") or ""
        pc = parse_clock((k.get("clock") or {}).get("displayValue", ""))
        if not pc:
            continue
        marca = (min(pc[0], DURACAO), tempo_real(pc[0], pc[1], h1, total) if total else None)
        cand = [str(x.get("athlete", {}).get("id")) for x in k.get("participants", []) or []
                if x.get("athlete") and str(x["athlete"].get("id")) in jogadores]
        if "substitution" in tipo:
            ent = sai = None
            dentro = [i for i in cand if i in em_campo]
            fora = [i for i in cand if i not in em_campo and i not in saida]
            if len(dentro) == 1 and len(fora) == 1:
                sai, ent = dentro[0], fora[0]
            else:
                m = re.search(r"Substitution,\s*(.+?)\.\s+(.+?) replaces (.+?)\s*$", texto)
                if m:
                    ent, sai = achar(m.group(2), m.group(1)), achar(m.group(3), m.group(1))
            if not ent or not sai or ent == sai:
                nao_casadas.append(f"{ev['data']} | {texto}")
                continue
            entrada.setdefault(ent, marca)
            saida[sai] = marca
            em_campo.discard(sai)
            em_campo.add(ent)
        elif re.search(r"\bred[- ]?card\b|second yellow|yellow[- ]red", tipo) and cand:
            if cand[0] in em_campo:
                expulso.setdefault(cand[0], marca)
                em_campo.discard(cand[0])

    linhas = []
    for aid, j in jogadores.items():
        if j["starter"]:
            ini, st = (0, 0), 0
        elif aid in entrada:
            ini, st = entrada[aid], 1
        elif j["subbedIn"]:
            ini, st = None, 1
            nao_casadas.append(f"{ev['data']} | entrada sem minuto: {j['nome']} ({j['time']})")
        else:
            ini, st = None, 2
        if ini is None:
            m90, mr = 0, (0 if total else None)
        else:
            fim90 = min(x[0] for x in (saida.get(aid), expulso.get(aid), (DURACAO, 0)) if x)
            m90 = max(0, fim90 - ini[0])
            if total:
                fimr = min(x[1] for x in (saida.get(aid), expulso.get(aid), (0, total)) if x)
                mr = max(0, fimr - (ini[1] or 0))
            else:
                mr = None
        linhas.append([j["time"], aid, j["nome"], j["pos"], st, m90, mr])

    alertas = []
    for t in times:
        soma = sum(l[5] for l in linhas if l[0] == t)
        perda = sum(DURACAO - expulso[a][0] for a in expulso if jogadores[a]["time"] == t)
        if soma != 11 * DURACAO - perda:
            alertas.append(f"{ev['data']} [{t}] soma {soma} ≠ {11 * DURACAO - perda}")
    return {"lados": lados, "duracao": total, "linhas": linhas,
            "alertas": alertas, "nao_casadas": nao_casadas}


# ------------------------------------------------------------------ posições
def atualizar_posicoes(sugestoes):
    existentes = {}
    if POSICOES.exists():
        with open(POSICOES, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                existentes[(r["time"], r["atleta_id"])] = r
    for (t, aid), (nome, pos) in sugestoes.items():
        manual = (existentes.get((t, aid), {}).get("grupo_manual") or "").strip().upper()
        existentes[(t, aid)] = {"time": t, "atleta_id": aid, "atleta": nome, "posicao_espn": pos,
                                "grupo_sugerido": grupo_de(pos), "grupo_manual": manual}
    ordem = {g: i for i, g in enumerate(ORDEM_GRUPOS)}
    linhas = sorted(existentes.values(),
                    key=lambda r: (r["time"], ordem.get(r["grupo_manual"] or r["grupo_sugerido"], 99), r["atleta"]))
    with open(POSICOES, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["time", "atleta_id", "atleta", "posicao_espn",
                                          "grupo_sugerido", "grupo_manual"], delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    return {k: (v["grupo_manual"] or v["grupo_sugerido"]) for k, v in existentes.items()}


# ------------------------------------------------------------------ execução
def main():
    SAIDA.mkdir(parents=True, exist_ok=True)
    limite = (date.today() - timedelta(days=DIAS_PARA_FIXAR)).isoformat()
    jogos, alertas, nao_casadas = [], [], []

    for liga, nome_liga in LIGAS.items():
        base, eventos = listar_eventos(liga)
        pasta = CACHE / liga / "jogos"
        pasta.mkdir(parents=True, exist_ok=True)
        encerrados = sorted((e for e in eventos.values() if e["encerrado"]), key=lambda e: e["data"])
        novos = 0
        for ev in encerrados:
            arq = pasta / f"{ev['id']}.json"
            if arq.exists():
                res = json.loads(arq.read_text(encoding="utf-8"))
            else:
                det = get(f"{base}/summary", event=ev["id"])
                time.sleep(0.3)
                if not det or not det.get("rosters"):
                    alertas.append(f"{ev['data']} jogo {ev['id']}: sem escalação no ESPN")
                    continue
                res = processar_jogo(ev, det)
                novos += 1
                if ev["data"] <= limite:
                    arq.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            alertas += res["alertas"]
            nao_casadas += res["nao_casadas"]
            jogos.append((liga, ev, res))
        print(f"[{liga}] {len(encerrados)} jogos encerrados, {novos} processados agora")

    # sugestão de posição: posição mais frequente em campo (ignora SUB)
    pos_cont = defaultdict(Counter)
    nomes = {}
    for _, _, res in jogos:
        for t, aid, nome, pos, st, *_ in res["linhas"]:
            nomes[(t, aid)] = nome
            if st != 2 and pos not in ("", "SUB"):
                pos_cont[(t, aid)][pos] += 1
    sugestoes = {k: (n, pos_cont[k].most_common(1)[0][0] if pos_cont[k] else "—")
                 for k, n in nomes.items()}
    grupos = atualizar_posicoes(sugestoes)

    # dados compactos para o dashboard
    ligas = list(LIGAS)
    times = sorted({t for _, _, r in jogos for t in r["lados"].values()})
    ti = {t: i for i, t in enumerate(times)}
    chaves = sorted(nomes, key=lambda k: (k[0], nomes[k]))
    ai = {k: i for i, k in enumerate(chaves)}
    atletas = [[nomes[k], ti[k[0]], grupos.get(k, "—"), sugestoes[k][1]] for k in chaves]
    lista_jogos, part = [], []
    for liga, ev, res in sorted(jogos, key=lambda x: x[1]["data"]):
        casa, fora = res["lados"].get("home"), res["lados"].get("away")
        if casa not in ti or fora not in ti:
            continue
        gi = len(lista_jogos)
        lista_jogos.append([ligas.index(liga), ev["data"], ti[casa], ti[fora],
                            ev.get("gols_casa"), ev.get("gols_fora"), res["duracao"]])
        for t, aid, nome, pos, st, m90, mr in res["linhas"]:
            part.append([gi, ai[(t, aid)], st, m90, mr])

    dados = {
        "atualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "temporada": ANO,
        "competicoes": [LIGAS[l] for l in ligas],
        "grupos": ORDEM_GRUPOS,
        "times": times, "atletas": atletas, "jogos": lista_jogos, "p": part,
    }
    (SAIDA / "dados.json").write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")
    (CACHE / "auditoria.txt").write_text(
        f"ALERTAS ({len(alertas)})\n" + "\n".join(alertas) +
        f"\n\nTROCAS NÃO CASADAS ({len(nao_casadas)})\n" + "\n".join(nao_casadas), encoding="utf-8")
    print(f"OK: {len(lista_jogos)} jogos, {len(atletas)} atletas, {len(alertas)} alertas, "
          f"{len(nao_casadas)} trocas não casadas")


if __name__ == "__main__":
    main()
