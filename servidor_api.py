"""
Servidor API Local — Explorador de Arquivos do Servidor
Roda em localhost:5123, permite a UI navegar nas pastas dos clientes no servidor.
"""

import os
import re
import json
import time
import logging
import unicodedata
import threading
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("agente-varredura")

# Preenchido pelo agente.py antes de subir a API (o RNX mostra no modal do agente)
VERSAO_AGENTE = None

# Importa funções de classificação do scan_documentos
from scan_documentos import (
    classificar_arquivo,
    extrair_subtipo_cnd,
    extrair_subtipo_alvara,
    extrair_subtipo_certificado,
    extrair_subtipo_nota_fiscal,
    extrair_subtipo_relatorio,
    extrair_validade_do_nome,
    extrair_periodo_do_nome,
    _montar_nome_display,
    _sanitizar_nome_storage,
    _content_type,
    _match_score,
    _normalizar,
    _limpar_nome,
    EXTENSOES_ACEITAS,
    PASTA_SERVIDOR,
)

# ============================================================
# SEGURANÇA: paths permitidos
# ============================================================

def _caminho_seguro(path_str: str) -> bool:
    """Valida que o caminho está dentro do servidor permitido.
    Previne path traversal e acesso a pastas fora do escopo."""
    try:
        resolved = Path(path_str).resolve()
        servidor_resolved = PASTA_SERVIDOR.resolve()
        return str(resolved).lower().startswith(str(servidor_resolved).lower())
    except (ValueError, OSError):
        return False


def _tokens_nome(s):
    """Tokens significativos do nome (sem stop words, >= 2 chars)."""
    return set(t for t in _limpar_nome(s or "").split() if len(t) >= 2)


def _resolver_pasta_cliente(cliente, supabase_client):
    """Resolve a pasta do servidor para um cliente.
    1) Match estrito por substring (>= 90) — comportamento original.
    2) Fallback por TOKENS (seguro): a pasta casa se TODOS os tokens significativos
       do nome mais curto estiverem no mais longo, com >= 2 tokens em comum.
       Se 2+ pastas empatam no topo, ABSTÉM (retorna None) pra não arquivar na errada."""
    if not PASTA_SERVIDOR.exists():
        return None

    try:
        pastas = [p for p in PASTA_SERVIDOR.iterdir() if p.is_dir()]
    except (PermissionError, OSError):
        return None

    razao = cliente.get("razaoSocial", "") or ""
    fantasia = cliente.get("nomeFantasia", "") or ""

    # 1) Estrito (substring)
    melhor_score = 0
    melhor_pasta = None
    for pasta in pastas:
        score = max(
            _match_score(pasta.name, razao),
            _match_score(pasta.name, fantasia) if fantasia else 0,
        )
        if score > melhor_score:
            melhor_score = score
            melhor_pasta = pasta

    if melhor_score >= 90:
        return melhor_pasta

    # 2) Fallback por tokens (conservador)
    nomes_cliente = [n for n in (razao, fantasia) if n]
    melhor_inter = 0
    candidatas = set()
    for pasta in pastas:
        tp = _tokens_nome(pasta.name)
        if not tp:
            continue
        for nome in nomes_cliente:
            tc = _tokens_nome(nome)
            if len(tc) < 2:
                continue
            menor = tp if len(tp) <= len(tc) else tc
            maior = tc if menor is tp else tp
            if len(menor) < 2:
                continue
            inter = len(tp & tc)
            # exige TODOS os tokens do menor no maior + >= 2 em comum
            if menor.issubset(maior) and inter >= 2:
                if inter > melhor_inter:
                    melhor_inter = inter
                    candidatas = {pasta}
                elif inter == melhor_inter:
                    candidatas.add(pasta)

    # Só retorna com UM vencedor claro (sem empate)
    if melhor_inter >= 2 and len(candidatas) == 1:
        return next(iter(candidatas))
    return None


# ============================================================
# ÍCONES POR EXTENSÃO
# ============================================================

_ICONES = {
    ".pdf": "far fa-file-pdf",
    ".doc": "far fa-file-word",
    ".docx": "far fa-file-word",
    ".xls": "far fa-file-excel",
    ".xlsx": "far fa-file-excel",
    ".jpg": "far fa-file-image",
    ".jpeg": "far fa-file-image",
    ".png": "far fa-file-image",
    ".pfx": "fas fa-key",
    ".p12": "fas fa-key",
    ".crt": "fas fa-certificate",
    ".cer": "fas fa-certificate",
    ".pem": "fas fa-certificate",
    ".txt": "far fa-file-alt",
    ".csv": "fas fa-table",
    ".xml": "fas fa-code",
    ".zip": "far fa-file-archive",
    ".rar": "far fa-file-archive",
}


def _icone_ext(ext: str) -> str:
    return _ICONES.get(ext.lower(), "far fa-file")


# ============================================================
# CRIAR APP FASTAPI
# ============================================================

def criar_app(supabase_client, config):
    """Cria e retorna a instância FastAPI."""
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
    from pydantic import BaseModel

    app = FastAPI(title="Agente Varredura API", docs_url=None, redoc_url=None)

    # CORS (1.1.9, 18/09/2026): antes era allow_origins=["*"] — QUALQUER site aberto
    # no navegador podia ler as pastas do servidor pelo /api/explorar. Agora so o
    # RNX (producao e previas do Netlify) e paginas locais.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(https://([a-z0-9-]+--)?routineex\.netlify\.app|http://(localhost|127\.0\.0\.1)(:\d+)?)$",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Cache de mapeamento cliente_id → pasta do servidor
    _cache_pastas: dict[int, Path | None] = {}
    _cache_clientes: dict[int, dict] = {}

    def _get_cliente(cliente_id: int) -> dict | None:
        if cliente_id in _cache_clientes:
            return _cache_clientes[cliente_id]
        try:
            resp = supabase_client.table("clientes").select("*").eq("id", cliente_id).execute()
            if resp.data:
                _cache_clientes[cliente_id] = resp.data[0]
                return resp.data[0]
        except Exception:
            pass
        return None

    def _get_pasta_cliente(cliente_id: int) -> Path | None:
        if cliente_id in _cache_pastas:
            return _cache_pastas[cliente_id]
        cliente = _get_cliente(cliente_id)
        if not cliente:
            return None
        pasta = _resolver_pasta_cliente(cliente, supabase_client)
        _cache_pastas[cliente_id] = pasta
        return pasta

    # ----------------------------------------------------------
    # GET /api/status
    # ----------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        return {
            "online": True,
            "servidor_acessivel": PASTA_SERVIDOR.exists(),
            "timestamp": datetime.now().isoformat(),
            # O RNX pergunta isto antes de mostrar "Consultar na Receita":
            # agente antigo nao tem a rota e o botao nao pode prometer o que nao ha.
            "recursos": ["explorador", "receita_cnpj", "situacao_fiscal", "carteira", "guia", "envios"],
            "versao": VERSAO_AGENTE,
            "usuario": config.get("user_nome") or config.get("user_email"),
            "usuario_id": config.get("user_id"),
        }

    # ----------------------------------------------------------
    # POST /api/fiscal/situacao   (18/09/2026)
    # A Central de Automacoes entrega aqui o resultado da consulta de situacao
    # fiscal (registros do dados-painel.json). O agente, que ja esta logado,
    # grava no RNX pela funcao rnx_registrar_situacao_fiscal — a Central nao
    # guarda senha nem chave do RNX. O banco decide pendencia, tarefa e aviso.
    # Exige o cabecalho X-RNX-Local: navegador de outro site nao consegue mandar
    # cabecalho proprio sem passar pelo CORS acima.
    # ----------------------------------------------------------
    from fastapi import Body, Header

    # So o que o RNX usa. Debitos e suspensos vao enxutos (ver _enxugar, 1.1.14).
    _CAMPOS_FISCAIS = ("cnpj", "empresa", "consultadoEm", "risco", "totalDebitos", "quantidadeDebitos",
                       "totalSuspenso", "quantidadeSuspensos", "inscricoesPgfn", "simples", "certidao",
                       "problemas", "arquivo", "origem", "versaoAnalise")

    # ----------------------------------------------------------
    # GET /api/carteira   (21/09/2026)
    # A Central de Automacoes le daqui a lista de clientes (antes vinha de uma
    # planilha Excel atualizada a mao). Sao os clientes ATIVOS da carteira de quem
    # esta logado no agente; sem vinculo nenhum, os que o RNX deixar ver.
    # ----------------------------------------------------------
    def _primeiro(c, *campos):
        for k in campos:
            v = c.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    @app.get("/api/carteira")
    def api_carteira(x_rnx_local: str | None = Header(default=None)):
        if x_rnx_local != "1":
            raise HTTPException(403, "Cabecalho X-RNX-Local ausente")
        uid = config.get("user_id")
        try:
            ids = []
            if uid:
                v = supabase_client.table("usuario_clientes").select("cliente_id").eq("usuario_id", uid).execute()
                ids = [r["cliente_id"] for r in (v.data or []) if r.get("cliente_id")]
            linhas = []
            if ids:
                for i in range(0, len(ids), 200):
                    r = supabase_client.table("clientes").select("*").in_("id", ids[i:i + 200]).execute()
                    linhas += r.data or []
            else:
                linhas = supabase_client.table("clientes").select("*").execute().data or []
        except Exception as e:
            raise HTTPException(502, f"Nao consegui ler a carteira no RNX: {str(e)[:200]}")

        clientes = []
        for c in linhas:
            status = _primeiro(c, "status") or "ATIVO"
            if "inativ" in status.lower():
                continue
            cnpj = re.sub(r"\D", "", c.get("cnpj") or "")
            if len(cnpj) != 14:
                continue
            clientes.append({
                "nome": _primeiro(c, "razaoSocial", "nomeFantasia"),
                "cnpj": cnpj,
                "regime": _primeiro(c, "regimeTributario", "regime"),
                "cidade": _primeiro(c, "cidade", "municipio"),
                "uf": _primeiro(c, "uf", "estado"),
                "status": status.upper(),
                "whatsapp": re.sub(r"\D", "", _primeiro(c, "whatsapp", "celular", "telefone")),
                "rnx_id": c.get("id"),
            })
        clientes.sort(key=lambda x: x["nome"])
        return {"usuario": config.get("user_nome") or config.get("user_email"),
                "carteira": bool(ids), "total": len(clientes), "clientes": clientes}

    # ----------------------------------------------------------
    # POST /api/guia   (21/09/2026)
    # O modulo de guias de antecipacao ICMS (Central) avisa aqui cada guia EMITIDA.
    # O agente sobe o PDF e o RNX cria a tarefa "GUIA ANTECIPACAO ICMS" ja realizada
    # e com "guia nao enviada" (rnx_registrar_guia_central). Cliente de outra carteira
    # vai para a triagem do dono. Nao ha tarefa mensal: a cobranca e a DeSTDA.
    # ----------------------------------------------------------
    def _subir_pdf_guia(caminho: Path, cnpj: str, competencia: str) -> str:
        bucket = config.get("bucket_storage", "documentos-clientes")
        prefixo = config.get("storage_path_prefix", "pdfs")
        comp = re.sub(r"\D", "", competencia or "")
        cnpj_dig = re.sub(r"\D", "", cnpj or "")
        nome = f"guias/ga379_{cnpj_dig}_{comp}_{int(time.time())}.pdf"
        with open(caminho, "rb") as f:
            supabase_client.storage.from_(bucket).upload(
                f"{prefixo}/{nome}", f.read(),
                file_options={"cache-control": "3600", "upsert": "true", "content-type": "application/pdf"})
        return supabase_client.storage.from_(bucket).get_public_url(f"{prefixo}/{nome}")

    def _uma_guia(g: dict) -> dict:
        base = {k: g.get(k) for k in ("cnpj", "competencia", "valor", "vencimento", "arquivo")}
        try:
            v = supabase_client.rpc("rnx_registrar_guia_central", {"p": dict(base, verificar=True)}).execute().data or {}
        except Exception as e:
            return {"ok": False, "cnpj": base.get("cnpj"), "motivo": str(e)[:200]}
        outra = (not v.get("ok")) and v.get("motivo") == "outra carteira"
        if not v.get("ok") and not outra:
            return dict(v, cnpj=base.get("cnpj"))

        arquivo = Path(str(base.get("arquivo") or ""))
        if arquivo.suffix.lower() != ".pdf" or not arquivo.is_file():
            return {"ok": False, "cnpj": base.get("cnpj"), "motivo": f"PDF da guia nao encontrado: {arquivo}"}
        try:
            pdf_url = _subir_pdf_guia(arquivo, base.get("cnpj"), base.get("competencia"))
            if outra:
                comp = str(base.get("competencia") or "")
                if re.match(r"^\d{2}/\d{4}$", comp):
                    comp = comp[3:] + "-" + comp[:2]
                r = supabase_client.rpc("rnx_encaminhar_triagem_outra_carteira", {"p": {
                    "cnpj": base.get("cnpj"), "nome_arquivo": arquivo.name, "tipo_guia": "GA 379",
                    "competencia": comp, "valor": base.get("valor"), "pdf_url": pdf_url,
                    "texto_extraido": "Guia de antecipacao ICMS (GA 379) emitida pela Central."}}).execute().data or {}
                return dict(r, cnpj=base.get("cnpj"), triagem=True)
            r = supabase_client.rpc("rnx_registrar_guia_central", {"p": dict(base, pdf_url=pdf_url)}).execute().data or {}
            return dict(r, cnpj=base.get("cnpj"))
        except Exception as e:
            return {"ok": False, "cnpj": base.get("cnpj"), "motivo": str(e)[:200]}

    @app.post("/api/guia")
    def api_guia(corpo: dict = Body(...), x_rnx_local: str | None = Header(default=None)):
        if x_rnx_local != "1":
            raise HTTPException(403, "Cabecalho X-RNX-Local ausente")
        guias = corpo.get("guias")
        if not isinstance(guias, list) or not guias:
            raise HTTPException(400, "Envie {\"guias\": [...]}")
        if len(guias) > 100:
            raise HTTPException(413, "No maximo 100 guias por envio")
        resultados = [_uma_guia(g) for g in guias if isinstance(g, dict)]
        logger.info(f"[GUIA] {len(resultados)} guia(s) recebida(s) da Central: "
                    + "; ".join(f"{r.get('cliente') or r.get('cnpj')}: "
                                + ("triagem de " + str(r.get('dono')) if r.get('triagem') else
                                   ("tarefa " + str(r.get('tarefa')) if r.get('ok') else str(r.get('motivo'))))
                                for r in resultados))
        return {"resultados": resultados,
                "ok": sum(1 for r in resultados if r.get("ok")),
                "falhas": sum(1 for r in resultados if not r.get("ok"))}

    # ----------------------------------------------------------
    # POST /api/envios  e  POST /api/envios/{id}   (21/09/2026)
    # A ponte de WhatsApp da Central registra cada envio no rastreamento do RNX
    # (envios_log) ANTES de mandar, e conclui depois. Antes ela fazia login no RNX
    # com e-mail e SENHA lidos do config.json do agente; agora usa a sessao do agente.
    # ----------------------------------------------------------
    _CAMPOS_ENVIO = ("id", "cliente_id", "cliente_nome", "destinatario_numero", "canal", "tipo", "assunto",
                     "mensagem", "evento_id", "pdf_url", "pdf_nome", "usuario_nome", "status", "enviado_em")
    _CAMPOS_CONCLUSAO = ("status", "wpp_message_id", "erro_mensagem")

    @app.post("/api/envios")
    def api_envio_registrar(corpo: dict = Body(...), x_rnx_local: str | None = Header(default=None)):
        if x_rnx_local != "1":
            raise HTTPException(403, "Cabecalho X-RNX-Local ausente")
        linha = {k: corpo[k] for k in _CAMPOS_ENVIO if k in corpo}
        if not linha.get("cliente_id") or not linha.get("mensagem"):
            raise HTTPException(400, "cliente_id e mensagem sao obrigatorios")
        linha["usuario_id"] = config.get("user_id")
        try:
            supabase_client.table("envios_log").insert(linha).execute()
        except Exception as e:
            raise HTTPException(502, f"Nao consegui registrar o envio no RNX: {str(e)[:200]}")
        return {"ok": True, "id": linha.get("id")}

    @app.post("/api/envios/{envio_id}")
    def api_envio_concluir(envio_id: str, corpo: dict = Body(...), x_rnx_local: str | None = Header(default=None)):
        if x_rnx_local != "1":
            raise HTTPException(403, "Cabecalho X-RNX-Local ausente")
        campos = {k: corpo[k] for k in _CAMPOS_CONCLUSAO if k in corpo}
        if not campos:
            raise HTTPException(400, "Nada para atualizar")
        try:
            supabase_client.table("envios_log").update(campos).eq("id", envio_id).execute()
        except Exception as e:
            raise HTTPException(502, f"Nao consegui concluir o envio no RNX: {str(e)[:200]}")
        return {"ok": True}

    # def (nao async): o FastAPI roda numa thread e as gravacoes nao travam as outras rotas
    @app.post("/api/fiscal/situacao")
    def api_fiscal_situacao(corpo: dict = Body(...), x_rnx_local: str | None = Header(default=None)):
        if x_rnx_local != "1":
            raise HTTPException(403, "Cabecalho X-RNX-Local ausente")
        consultas = corpo.get("consultas")
        if not isinstance(consultas, list) or not consultas:
            raise HTTPException(400, "Envie {\"consultas\": [...]}")
        if len(consultas) > 500:
            raise HTTPException(413, "No maximo 500 consultas por envio")
        gerar = bool(corpo.get("gerar_tarefas", True))

        # Mais antiga primeiro: so a mais recente de cada cliente mexe nas pendencias
        consultas = sorted((c for c in consultas if isinstance(c, dict)),
                           key=lambda c: str(c.get("consultadoEm") or ""))
        resultados = []
        def _enxugar(itens, limite):
            # So o que a tarefa mostra (1.1.14): receita, periodo, vencimento e valores
            if not isinstance(itens, list):
                return []
            return [{k: i.get(k) for k in ("receita", "pa", "vcto", "devedor", "consolidado", "categoria")}
                    for i in itens[:limite] if isinstance(i, dict)]

        for c in consultas:
            registro = {k: c[k] for k in _CAMPOS_FISCAIS if k in c}
            registro["debitos"] = _enxugar(c.get("debitos"), 200)
            registro["suspensos"] = _enxugar(c.get("suspensos"), 100)
            try:
                r = supabase_client.rpc("rnx_registrar_situacao_fiscal",
                                        {"p": registro, "p_gerar_tarefas": gerar}).execute()
                resultados.append(r.data)
            except Exception as e:
                resultados.append({"ok": False, "cnpj": registro.get("cnpj"), "motivo": str(e)[:200]})
        tarefas = sorted({r.get("tarefa") for r in resultados if isinstance(r, dict) and r.get("tarefa")})
        resumo = {
            "recebidas": len(consultas),
            "gravadas": sum(1 for r in resultados if isinstance(r, dict) and r.get("consulta")),
            "repetidas": sum(1 for r in resultados if isinstance(r, dict) and r.get("repetida")),
            "sem_cliente": sum(1 for r in resultados if isinstance(r, dict) and r.get("consulta") and not r.get("cliente")),
            "erros": sum(1 for r in resultados if not (isinstance(r, dict) and r.get("ok"))),
            "tarefas": tarefas,
        }
        logger.info(f"[FISCAL] Situacao fiscal recebida: {resumo}")
        return {"resumo": resumo, "resultados": resultados}

    # ----------------------------------------------------------
    # GET /api/receita/cnpj/{cnpj}   (16/09/2026)
    # Abre a consulta oficial da Receita numa janela do Edge; a PESSOA resolve
    # o captcha; devolve o comprovante e o quadro de socios. Ver receita_cnpj.py.
    # Uma consulta por vez: duas janelas de captcha ao mesmo tempo so confundem.
    # ----------------------------------------------------------
    _trava_receita = threading.Lock()

    @app.get("/api/receita/cnpj/{cnpj}")
    def api_receita_cnpj(cnpj: str):
        if not _trava_receita.acquire(blocking=False):
            return JSONResponse(status_code=409, content={
                "erro": "Ja existe uma consulta na Receita aberta. Termine ou feche aquela janela."})
        try:
            from receita_cnpj import consultar
            resultado = consultar(cnpj, tempo_limite=300, log=logger.info)
            if "erro" in resultado:
                return JSONResponse(status_code=422, content=resultado)
            return resultado
        finally:
            _trava_receita.release()

    # ----------------------------------------------------------
    # GET /api/explorar?cliente_id=123&path=...
    # ----------------------------------------------------------
    @app.get("/api/explorar")
    def api_explorar(
        cliente_id: int = Query(...),
        path: str = Query(default=""),
    ):
        pasta_raiz = _get_pasta_cliente(cliente_id)
        if not pasta_raiz:
            raise HTTPException(404, "Pasta do cliente não encontrada no servidor")

        # Se path vazio, usa raiz do cliente
        if not path:
            pasta_alvo = pasta_raiz
        else:
            pasta_alvo = Path(path)

        # Segurança: path deve estar dentro da pasta do cliente
        try:
            pasta_alvo_resolved = pasta_alvo.resolve()
            pasta_raiz_resolved = pasta_raiz.resolve()
            if not str(pasta_alvo_resolved).lower().startswith(str(pasta_raiz_resolved).lower()):
                raise HTTPException(403, "Acesso negado: fora da pasta do cliente")
        except (ValueError, OSError):
            raise HTTPException(400, "Caminho inválido")

        if not pasta_alvo.exists():
            raise HTTPException(404, "Pasta não encontrada")

        try:
            itens = sorted(pasta_alvo.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError) as e:
            raise HTTPException(403, f"Sem permissão: {e}")

        resultado_pastas = []
        resultado_arquivos = []

        for item in itens:
            try:
                nome = item.name
                if item.is_dir():
                    # Conta arquivos dentro (1 nível)
                    try:
                        n_filhos = sum(1 for f in item.iterdir() if f.is_file())
                    except (PermissionError, OSError):
                        n_filhos = 0
                    resultado_pastas.append({
                        "nome": nome,
                        "tipo": "pasta",
                        "caminho": str(item),
                        "filhos": n_filhos,
                    })
                elif item.is_file():
                    ext = item.suffix.lower()
                    try:
                        stat = item.stat()
                        tamanho = stat.st_size
                        modificado = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    except (PermissionError, OSError):
                        tamanho = 0
                        modificado = None

                    # Classificação automática
                    tipo_doc = classificar_arquivo(nome)
                    nome_display = _montar_nome_display(tipo_doc, nome) if tipo_doc else None

                    resultado_arquivos.append({
                        "nome": nome,
                        "tipo": "arquivo",
                        "caminho": str(item),
                        "extensao": ext,
                        "tamanho": tamanho,
                        "modificado": modificado,
                        "icone": _icone_ext(ext),
                        "tipo_doc": tipo_doc,
                        "nome_display": nome_display,
                        "importavel": ext in EXTENSOES_ACEITAS,
                    })
            except (PermissionError, OSError):
                continue

        # Breadcrumb: partes do caminho relativo à raiz
        try:
            rel = pasta_alvo_resolved.relative_to(pasta_raiz_resolved)
            partes_breadcrumb = [{"nome": pasta_raiz.name, "caminho": str(pasta_raiz)}]
            acumulado = pasta_raiz
            for parte in rel.parts:
                acumulado = acumulado / parte
                partes_breadcrumb.append({"nome": parte, "caminho": str(acumulado)})
        except (ValueError, OSError):
            partes_breadcrumb = [{"nome": pasta_raiz.name, "caminho": str(pasta_raiz)}]

        return {
            "pasta_atual": str(pasta_alvo),
            "pasta_raiz": str(pasta_raiz),
            "breadcrumb": partes_breadcrumb,
            "pastas": resultado_pastas,
            "arquivos": resultado_arquivos,
            "total_pastas": len(resultado_pastas),
            "total_arquivos": len(resultado_arquivos),
        }

    # ----------------------------------------------------------
    # GET /api/preview?path=...&cliente_id=123
    # ----------------------------------------------------------
    @app.get("/api/preview")
    def api_preview(
        path: str = Query(...),
        cliente_id: int = Query(...),
    ):
        pasta_raiz = _get_pasta_cliente(cliente_id)
        if not pasta_raiz:
            raise HTTPException(404, "Cliente não encontrado")

        arquivo = Path(path)

        # Segurança
        try:
            if not str(arquivo.resolve()).lower().startswith(str(pasta_raiz.resolve()).lower()):
                raise HTTPException(403, "Acesso negado")
        except (ValueError, OSError):
            raise HTTPException(400, "Caminho inválido")

        if not arquivo.is_file():
            raise HTTPException(404, "Arquivo não encontrado")

        ext = arquivo.suffix.lower()
        ct = _content_type(ext)

        # Para imagens, serve direto
        if ext in (".jpg", ".jpeg", ".png"):
            return FileResponse(str(arquivo), media_type=ct)

        # Para PDFs, gera thumbnail da primeira página
        if ext == ".pdf":
            try:
                import pymupdf
                doc = pymupdf.open(str(arquivo))
                page = doc[0]
                pix = page.get_pixmap(dpi=120)
                img_bytes = pix.tobytes("png")
                doc.close()
                return StreamingResponse(
                    iter([img_bytes]),
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"},
                )
            except Exception as e:
                raise HTTPException(500, f"Erro ao gerar preview: {e}")

        raise HTTPException(415, "Preview não suportado para este tipo de arquivo")

    # ----------------------------------------------------------
    # POST /api/importar
    # ----------------------------------------------------------
    class ImportarRequest(BaseModel):
        cliente_id: int
        arquivos: list[dict]  # [{caminho, nome_display?, tipo_doc?}]

    @app.post("/api/importar")
    def api_importar(req: ImportarRequest):
        pasta_raiz = _get_pasta_cliente(req.cliente_id)
        if not pasta_raiz:
            raise HTTPException(404, "Cliente não encontrado")

        cliente = _get_cliente(req.cliente_id)
        razao = cliente.get("razaoSocial", "?") if cliente else "?"

        bucket = config.get("bucket_storage", "documentos-clientes")
        prefix = config.get("storage_path_prefix", "pdfs")

        # Busca docs/certs existentes do cliente
        try:
            resp = supabase_client.table("clientes") \
                .select("id,documentos,certificados") \
                .eq("id", req.cliente_id).execute()
            if not resp.data:
                raise HTTPException(404, "Cliente não encontrado no banco")
            cli_data = resp.data[0]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Erro ao buscar cliente: {e}")

        docs_existentes = cli_data.get("documentos") or []
        if isinstance(docs_existentes, str):
            try:
                docs_existentes = json.loads(docs_existentes)
            except (json.JSONDecodeError, TypeError):
                docs_existentes = []

        certs_existentes = cli_data.get("certificados") or []
        if isinstance(certs_existentes, str):
            try:
                certs_existentes = json.loads(certs_existentes)
            except (json.JSONDecodeError, TypeError):
                certs_existentes = []

        docs_novos = list(docs_existentes)
        certs_novos = list(certs_existentes)
        resultados = []
        houve_mudanca_docs = False
        houve_mudanca_certs = False

        for arq_info in req.arquivos:
            caminho = arq_info.get("caminho", "")
            arquivo = Path(caminho)

            # Segurança
            try:
                if not str(arquivo.resolve()).lower().startswith(str(pasta_raiz.resolve()).lower()):
                    resultados.append({"caminho": caminho, "status": "erro", "msg": "Acesso negado"})
                    continue
            except (ValueError, OSError):
                resultados.append({"caminho": caminho, "status": "erro", "msg": "Caminho inválido"})
                continue

            if not arquivo.is_file():
                resultados.append({"caminho": caminho, "status": "erro", "msg": "Arquivo não encontrado"})
                continue

            ext = arquivo.suffix.lower()
            tipo_doc = arq_info.get("tipo_doc") or classificar_arquivo(arquivo.name)
            nome_display = arq_info.get("nome_display") or (
                _montar_nome_display(tipo_doc, arquivo.name) if tipo_doc else arquivo.stem
            )

            is_cert = tipo_doc == "Certificado"
            storage_prefix = "certificados" if is_cert else prefix

            nome_storage = _sanitizar_nome_storage(
                f"{_sanitizar_nome_storage(razao)}_{tipo_doc or 'doc'}_{int(time.time())}_{_sanitizar_nome_storage(arquivo.stem)}{ext}"
            )
            storage_path = f"{storage_prefix}/{nome_storage}"

            try:
                with open(arquivo, "rb") as f:
                    supabase_client.storage.from_(bucket).upload(
                        storage_path, f.read(),
                        file_options={
                            "cache-control": "3600",
                            "upsert": "true",
                            "content-type": _content_type(ext),
                        },
                    )
                url = supabase_client.storage.from_(bucket).get_public_url(storage_path)
                if not url:
                    raise ValueError("URL não obtida")
            except Exception as e:
                resultados.append({"caminho": caminho, "status": "erro", "msg": f"Erro upload: {e}"})
                continue

            if is_cert:
                from scan_documentos import extrair_subtipo_certificado as _esc
                subtipo_cert = _esc(arquivo.name) or "A1"
                validade = extrair_validade_do_nome(arquivo.name)
                cert_final = {
                    "nome": nome_display,
                    "tipo": subtipo_cert,
                    "vencimento": validade,
                    "arquivo": {
                        "nome": arquivo.name,
                        "url": url,
                        "tamanho": arquivo.stat().st_size,
                        "uploadEm": datetime.now().isoformat(),
                    },
                }
                certs_novos.append(cert_final)
                houve_mudanca_certs = True
            else:
                validade = extrair_validade_do_nome(arquivo.name)
                doc_final = {
                    "nome": nome_display,
                    "tipo": tipo_doc or "Outro",
                    "validade": validade,
                    "url": url,
                    "uploadEm": datetime.now().isoformat(),
                    "origem": "explorador-servidor",
                }
                docs_novos.append(doc_final)
                houve_mudanca_docs = True

            resultados.append({
                "caminho": caminho,
                "status": "ok",
                "nome_display": nome_display,
                "tipo": tipo_doc or "Outro",
                "url": url,
            })
            logger.info(f"[EXPLORADOR] [{razao}] Importado: \"{nome_display}\" ({arquivo.name})")

        # Atualiza cliente no Supabase
        update_data = {}
        if houve_mudanca_docs:
            update_data["documentos"] = docs_novos
        if houve_mudanca_certs:
            update_data["certificados"] = certs_novos

        if update_data:
            try:
                supabase_client.table("clientes").update(update_data) \
                    .eq("id", req.cliente_id).execute()
            except Exception as e:
                logger.error(f"[EXPLORADOR] Erro ao salvar: {e}")
                raise HTTPException(500, f"Upload OK mas erro ao salvar no cliente: {e}")

        ok_count = sum(1 for r in resultados if r["status"] == "ok")
        err_count = sum(1 for r in resultados if r["status"] == "erro")

        return {
            "importados": ok_count,
            "erros": err_count,
            "resultados": resultados,
        }

    # ----------------------------------------------------------
    # POST /api/limpar-cache
    # ----------------------------------------------------------
    @app.post("/api/limpar-cache")
    def api_limpar_cache():
        _cache_pastas.clear()
        _cache_clientes.clear()
        return {"ok": True}

    return app


# ============================================================
# INICIAR SERVIDOR EM THREAD SEPARADA
# ============================================================

def iniciar_servidor(supabase_client, config, porta: int = 5123):
    """Inicia o servidor FastAPI em uma thread daemon."""
    try:
        import uvicorn
        app = criar_app(supabase_client, config)
        logger.info(f"[API] Servidor local iniciando em http://localhost:{porta}")

        server_config = uvicorn.Config(
            app,
            # 1.1.9: so este computador. Antes era 0.0.0.0 e qualquer maquina da
            # rede chamava o agente de outra pessoa (explorar pastas, importar).
            host="127.0.0.1",
            port=porta,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        server = uvicorn.Server(server_config)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        logger.info(f"[API] Servidor ativo na porta {porta}")
        return thread
    except ImportError:
        logger.warning("[API] fastapi/uvicorn não instalados. Servidor local desabilitado.")
        logger.warning("[API] Instale com: pip install fastapi uvicorn")
        return None
    except Exception as e:
        logger.error(f"[API] Erro ao iniciar servidor: {e}")
        return None
