"""
Scraper de Agenda Tributária - Busca obrigações do contadores.cnt.br e salva no Supabase.
Roda localmente (sem CORS, sem CloudFlare blocking).

Uso:
    python scraper_agenda.py                    # Busca federal do mês atual + próximo
    python scraper_agenda.py federal 2026 04    # Busca federal de abril/2026
    python scraper_agenda.py estadual 2026 04 rio-grande-do-sul   # Busca RS
    python scraper_agenda.py --auto             # Modo automático: federal + RS, mês atual + próximo
"""

import sys
import os
import re
import json
import html as html_module
import logging
import urllib.request
from datetime import datetime, date
from pathlib import Path

# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Mesma regra do agente.py: dentro do .exe, Path(__file__) aponta para a pasta
# temporaria que o PyInstaller cria e destroi, onde nao existe config.json.
# Era isso que derrubava o scraper com "No such file or directory ..._MEIxxxxx".
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RNX-Agente"
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
    CONFIG = json.load(f)

SUPABASE_URL = CONFIG["supabase_url"]
SUPABASE_KEY = CONFIG["supabase_key"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENDA] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("scraper_agenda")

# Estado padrão para estadual
ESTADO_PADRAO = "rio-grande-do-sul"
UF_PADRAO = "RS"

# ============================================================
# FETCH HTML (direto, sem proxy)
# ============================================================

def fetch_html(url: str, timeout: int = 30) -> str:
    """Busca HTML do site. Funciona direto do PC (sem CloudFlare block)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode("utf-8", errors="replace")


# ============================================================
# PARSER (mesma lógica do scraper-agenda.js)
# ============================================================

def decode_html(text: str) -> str:
    """Decodifica entidades HTML e limpa tags."""
    if not text:
        return ""
    # Decodifica entidades HTML padrão
    text = html_module.unescape(text)
    # Converte <br> em newline
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "", text, flags=re.I)
    # Remove todas as tags HTML restantes
    text = re.sub(r"<[^>]+>", "", text)
    # Limpa espaços
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _parsear_estrutura_nova(html_content: str, ano: int, mes: int) -> list:
    """
    Estrutura NOVA (2026) do contadores.cnt.br:
    <div ...content-dia__item id="dia-DD"> com header 'Dia <span>DD</span>' e vários
    <div ...blcitem> contendo blcitem__title (nome) e blcitem__descricao (descrição).
    O dia de cada item é o último cabeçalho de dia que aparece antes dele no HTML.
    """
    obrigacoes = []

    # Marcadores de dia (posição no texto -> número do dia)
    dias = [(m.start(), int(m.group(1)))
            for m in re.finditer(r'item-header--namedia[^>]*>\s*Dia\s*<span>\s*(\d+)\s*</span>',
                                 html_content, re.I)]
    if not dias:
        return obrigacoes

    def dia_da_pos(pos: int):
        d = None
        for start, dd in dias:
            if start <= pos:
                d = dd
            else:
                break
        return d

    item_re = re.compile(
        r'blcitem__title[^>]*>(.*?)</div>\s*<div[^>]*blcitem__descricao[^>]*>(.*?)</div>',
        re.S | re.I
    )
    for m in item_re.finditer(html_content):
        nome = decode_html(m.group(1))
        descricao = decode_html(m.group(2))
        if nome and len(nome) > 2:
            dia = dia_da_pos(m.start())
            if not dia:
                continue
            obrigacoes.append({
                "nome": nome,
                "descricao": descricao or "Obrigação tributária",
                "data": f"{ano}-{mes:02d}-{dia:02d}",
                "dia": dia,
                "mes": mes,
                "ano": ano,
            })

    return obrigacoes


def _parsear_estrutura_legada(html_content: str, ano: int, mes: int) -> list:
    """Layout ANTIGO: <tbody class="tributos-do-dia" id="DD"> → <tr class="tributo"> → <td class="titulo">/<td class="conteudo">."""
    obrigacoes = []
    tbody_pattern = re.compile(
        r'<tbody\s+class="tributos-do-dia"\s+id="(\d+)">(.*?)</tbody>',
        re.S | re.I
    )
    for tbody_match in tbody_pattern.finditer(html_content):
        dia = int(tbody_match.group(1))
        tbody_content = tbody_match.group(2)
        tr_pattern = re.compile(
            r'<tr\s+class="tributo"[^>]*>[\s\S]*?'
            r'<td\s+class="titulo[^"]*">([\s\S]*?)</td>\s*'
            r'<td\s+class="conteudo[^"]*">([\s\S]*?)</td>'
            r'[\s\S]*?</tr>',
            re.I
        )
        for tr_match in tr_pattern.finditer(tbody_content):
            nome = decode_html(tr_match.group(1))
            descricao = decode_html(tr_match.group(2))
            if nome and len(nome) > 2:
                obrigacoes.append({
                    "nome": nome,
                    "descricao": descricao or "Obrigação tributária",
                    "data": f"{ano}-{mes:02d}-{dia:02d}",
                    "dia": dia,
                    "mes": mes,
                    "ano": ano,
                })
    return obrigacoes


def parsear_tabela(html_content: str, ano: int, mes: int) -> list:
    """Tenta a estrutura NOVA (2026) e cai pro layout ANTIGO se não achar nada."""
    obrigacoes = _parsear_estrutura_nova(html_content, ano, mes)
    if obrigacoes:
        return obrigacoes
    log.info("Estrutura nova não encontrada; tentando layout antigo (tabela)...")
    return _parsear_estrutura_legada(html_content, ano, mes)


# ============================================================
# SUPABASE REST API (sem SDK – evita incompatibilidade Python 3.13)
# ============================================================

def _supabase_request(method: str, table: str, data=None, params: str = "") -> dict:
    """Faz request direto na REST API do Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{table}{params}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if method == "UPSERT":
        method = "POST"
        headers["Prefer"] = "resolution=merge-duplicates"

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        if resp.status in (200, 201, 204):
            content = resp.read().decode("utf-8")
            return json.loads(content) if content.strip() else {}
        return {}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        log.error(f"Supabase {method} {table}: HTTP {e.code} - {body_err[:200]}")
        raise


def salvar_no_cache(obrigacoes: list, tipo: str, estado: str = None) -> int:
    """Salva obrigações no agenda_cache. Período já foi limpo antes."""
    if not obrigacoes:
        return 0

    # Deduplica por (dia, nome) — site pode ter entradas repetidas
    vistos = set()
    registros = []
    for ob in obrigacoes:
        chave = (ob["dia"], ob["nome"])
        if chave in vistos:
            continue
        vistos.add(chave)
        registros.append({
            "tipo": tipo,
            "estado": estado,
            "ano": ob["ano"],
            "mes": ob["mes"],
            "dia": ob["dia"],
            "nome": ob["nome"],
            "descricao": ob["descricao"],
            "data": ob["data"],
            "atualizado_em": datetime.now().isoformat(),
        })

    # INSERT em lotes de 50
    total = 0
    for i in range(0, len(registros), 50):
        lote = registros[i:i + 50]
        try:
            _supabase_request("POST", "agenda_cache", lote)
            total += len(lote)
        except Exception as e:
            log.error(f"Erro ao inserir lote: {e}")

    return total


# ============================================================
# BUSCA PRINCIPAL
# ============================================================

def buscar_e_cachear(tipo: str, ano: int, mes: int, estado: str = None) -> int:
    """Busca obrigações do site e salva no cache. Retorna quantidade."""
    if tipo == "federal":
        url = f"https://www.contadores.cnt.br/agenda-tributaria/federal/{ano}/{mes:02d}.html"
    else:
        if not estado:
            log.error("Estado é obrigatório para tipo estadual")
            return 0
        url = f"https://www.contadores.cnt.br/agenda-tributaria/estadual/{estado}/{ano}/{mes:02d}.html"

    label = f"{tipo.upper()} {ano}/{mes:02d}" + (f" ({estado})" if estado else "")
    log.info(f"Buscando {label} de {url}")

    try:
        html_content = fetch_html(url)
        log.info(f"HTML baixado: {len(html_content)} chars")
    except Exception as e:
        log.error(f"Erro ao buscar {label}: {e}")
        return 0

    if len(html_content) < 5000:
        log.warning(f"HTML muito curto ({len(html_content)} chars) - site pode não ter dados")
        return 0

    obrigacoes = parsear_tabela(html_content, ano, mes)
    log.info(f"Parser extraiu {len(obrigacoes)} obrigações de {label}")

    if not obrigacoes:
        log.warning(f"Nenhuma obrigação encontrada para {label}")
        return 0

    # Limpa cache antigo desse período antes de inserir
    try:
        params = f"?tipo=eq.{tipo}&ano=eq.{ano}&mes=eq.{mes}"
        if estado:
            params += f"&estado=eq.{estado}"
        else:
            params += "&estado=is.null"
        _supabase_request("DELETE", "agenda_cache", params=params)
    except Exception as e:
        log.warning(f"Erro ao limpar cache antigo: {e}")

    total = salvar_no_cache(obrigacoes, tipo, estado)
    log.info(f"✅ {total} obrigações salvas no cache para {label}")

    # Log resumo por dia
    dias = sorted(set(o["dia"] for o in obrigacoes))
    log.info(f"   Dias com obrigações: {', '.join(str(d) for d in dias)}")

    return total


def modo_automatico():
    """Busca federal + estadual (RS) para mês atual e próximo."""
    hoje = date.today()
    ano = hoje.year
    mes = hoje.month

    # Mês seguinte
    prox_mes = mes + 1 if mes < 12 else 1
    prox_ano = ano if mes < 12 else ano + 1

    total_geral = 0

    # Federal: mês atual + próximo
    for a, m in [(ano, mes), (prox_ano, prox_mes)]:
        total_geral += buscar_e_cachear("federal", a, m)

    # Estadual RS: mês atual + próximo
    for a, m in [(ano, mes), (prox_ano, prox_mes)]:
        total_geral += buscar_e_cachear("estadual", a, m, ESTADO_PADRAO)

    log.info(f"{'=' * 50}")
    log.info(f"✅ TOTAL GERAL: {total_geral} obrigações cacheadas")
    return total_geral


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "--auto":
        modo_automatico()
    elif len(args) >= 3:
        tipo = args[0]  # federal ou estadual
        ano = int(args[1])
        mes = int(args[2])
        estado = args[3] if len(args) > 3 else None
        buscar_e_cachear(tipo, ano, mes, estado)
    else:
        print(__doc__)
        sys.exit(1)
