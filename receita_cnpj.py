"""
CONSULTA DE CNPJ NA RECEITA, COM O CAPTCHA RESOLVIDO POR UMA PESSOA  (16/09/2026)

POR QUE ISTO EXISTE
As APIs publicas (BrasilAPI, ReceitaWS) usam a base de dados abertos que a
Receita publica de tempos em tempos: empresa recem-aberta nao existe nelas. A
fonte em tempo real e a consulta oficial
    https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/
que tem captcha (hCaptcha). O Dominio faz o mesmo que este modulo: abre a
pagina, a pessoa resolve o captcha, e o programa le o resultado.

Um site (o RNX no navegador) nao pode fazer isso: o navegador proibe um site de
ler outro. O agente e um programa instalado, entao pode.

COMO FUNCIONA
1. Abre o Microsoft Edge em modo janela (--app), com perfil proprio e a porta de
   depuracao (CDP) ligada, ja na pagina com ?cnpj=... (a pagina preenche sozinha).
2. A PESSOA resolve o captcha e clica em Consultar. Nada aqui contorna o captcha.
3. A pagina chama a API interna da Receita:
      POST consultapublica/validar-captcha  -> header Session-Token
      GET  consultapublica/cnpj/{cnpj}       -> JSON do comprovante
   O agente escuta a rede da propria janela (Network.*) e copia essa resposta.
4. Com o mesmo Session-Token, pede o quadro de socios (consultapublica/qsa),
   de dentro da pagina, como o botao "Consultar QSA" faria.
5. Fecha a janela e devolve {"cnpj": {...}, "qsa": {...}}.

Teste manual:  python receita_cnpj.py 35933666000187
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

URL_CONSULTA = "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/?cnpj={}"
CAMINHOS_EDGE = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _porta_livre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


def _pasta_perfil():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    pasta = os.path.join(base, "RNX-Agente", "receita-edge")
    os.makedirs(pasta, exist_ok=True)
    return pasta


def consultar(cnpj, tempo_limite=300, log=print):
    """Devolve {"cnpj": dict, "qsa": dict|None} ou {"erro": "..."}."""
    cnpj = re.sub(r"\D", "", str(cnpj or ""))
    if len(cnpj) != 14:
        return {"erro": "CNPJ deve ter 14 digitos"}

    edge = next((p for p in CAMINHOS_EDGE if os.path.exists(p)), None)
    if not edge:
        return {"erro": "Microsoft Edge nao encontrado neste computador"}

    porta = _porta_livre()
    proc = subprocess.Popen([
        edge,
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={_pasta_perfil()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=860,920",
        f"--app={URL_CONSULTA.format(cnpj)}",
    ])
    log(f"[receita] janela aberta (CNPJ {cnpj}); aguardando o captcha")

    try:
        ws_url = None
        for _ in range(150):
            try:
                alvos = httpx.get(f"http://127.0.0.1:{porta}/json/list", timeout=2).json()
                paginas = [a for a in alvos if a.get("type") == "page" and "receita" in a.get("url", "")]
                if paginas:
                    ws_url = paginas[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not ws_url:
            return {"erro": "nao consegui abrir a pagina da Receita"}

        with connect(ws_url, max_size=32 * 1024 * 1024, open_timeout=15) as ws:
            seq = [0]
            fila = []

            def enviar(metodo, params=None):
                seq[0] += 1
                ws.send(json.dumps({"id": seq[0], "method": metodo, "params": params or {}}))
                return seq[0]

            def aguardar(idx, limite=30):
                fim = time.time() + limite
                while time.time() < fim:
                    try:
                        msg = json.loads(ws.recv(timeout=max(0.1, fim - time.time())))
                    except TimeoutError:
                        break
                    if msg.get("id") == idx:
                        return msg
                    fila.append(msg)
                return None

            aguardar(enviar("Network.enable"))

            token = None
            req_cnpj = None
            url_cnpj = None
            fim = time.time() + tempo_limite
            while time.time() < fim:
                if fila:
                    msg = fila.pop(0)
                else:
                    try:
                        msg = json.loads(ws.recv(timeout=1))
                    except TimeoutError:
                        continue

                metodo = msg.get("method")
                if metodo == "Network.responseReceived":
                    resp = msg["params"]["response"]
                    url = resp.get("url", "")
                    if "consultapublica/validar-captcha" in url:
                        cab = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
                        token = cab.get("session-token") or token
                        log("[receita] captcha validado")
                    elif "consultapublica/cnpj/" in url and resp.get("status") == 200:
                        req_cnpj = msg["params"]["requestId"]
                        url_cnpj = url

                elif metodo == "Network.loadingFinished" and req_cnpj and msg["params"]["requestId"] == req_cnpj:
                    r = aguardar(enviar("Network.getResponseBody", {"requestId": req_cnpj}))
                    corpo = ((r or {}).get("result") or {}).get("body") or ""
                    if ((r or {}).get("result") or {}).get("base64Encoded"):
                        import base64
                        corpo = base64.b64decode(corpo).decode("utf-8", "replace")
                    try:
                        dados = json.loads(corpo)
                    except Exception:
                        return {"erro": "a Receita respondeu num formato inesperado"}
                    log("[receita] comprovante capturado")

                    qsa = None
                    if token and url_cnpj:
                        base = url_cnpj.split("consultapublica/")[0]
                        js = ("fetch(%s,{headers:{'Session-Token':%s},credentials:'include'})"
                              ".then(function(r){return r.ok?r.text():''}).catch(function(){return ''})"
                              % (json.dumps(base + "consultapublica/qsa/" + cnpj), json.dumps(token)))
                        r2 = aguardar(enviar("Runtime.evaluate",
                                             {"expression": js, "awaitPromise": True, "returnByValue": True}))
                        try:
                            texto = r2["result"]["result"]["value"]
                            qsa = json.loads(texto) if texto else None
                        except Exception:
                            qsa = None

                    try:
                        enviar("Browser.close")
                    except Exception:
                        pass
                    return {"cnpj": dados, "qsa": qsa}

            return {"erro": "tempo esgotado: o captcha nao foi resolvido"}

    except ConnectionClosed:
        return {"erro": "a janela da Receita foi fechada antes da consulta"}
    except Exception as e:
        return {"erro": f"falha na consulta: {e}"}
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    resultado = consultar(sys.argv[1] if len(sys.argv) > 1 else "")
    saida = sys.argv[2] if len(sys.argv) > 2 else None
    texto = json.dumps(resultado, ensure_ascii=False, indent=2)
    if saida:
        with open(saida, "w", encoding="utf-8") as f:
            f.write(texto)
    print(texto[:3000])
