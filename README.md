# Agente de Varredura — RNX

Monitora uma pasta, lê PDFs de guias fiscais (DAS, DARF, GPS, DESTDA...),
identifica o cliente pelo CNPJ e faz a baixa automática no RNX.

## Instalação

1. Baixe o `AgenteVarredura.exe` na [última release](https://github.com/lukas913/rnx-agente-varredura/releases/latest).
2. Execute. Não precisa instalar Python nem nada.
3. Na primeira vez, informe seu **e-mail e senha do RNX** e escolha a pasta a monitorar.

O agente se registra sozinho para iniciar junto com o Windows.

Seu usuário precisa ter a funcionalidade `agente_varredura` liberada no RNX
(quem é `gerente` já tem acesso).

## Onde ficam os dados

Tudo em `%LOCALAPPDATA%\RNX-Agente\`:

| | |
|---|---|
| `config.json` | credenciais e pastas configuradas |
| `_logs\` | um arquivo por dia |

O `.exe` pode ficar em qualquer lugar — Downloads, Área de Trabalho, Program
Files. Os dados não moram junto dele.

> **Por que assim:** até a v1.0.0 os dados ficavam ao lado do executável, e numa
> build eles caíam na pasta temporária que o PyInstaller cria e apaga. O agente
> perdia a configuração e rodava sem autenticação, sem avisar ninguém.

## Atualização automática

A partir da v1.1.0 o agente consulta a release mais recente deste repositório
toda vez que inicia. Se houver versão nova, ele baixa, confere o tamanho, se
substitui e reinicia sozinho.

Sem internet ou com o GitHub fora do ar, ele apenas registra o aviso no log e
continua trabalhando na versão atual.

## Publicando uma versão nova

1. Suba `VERSAO` no topo do `agente.py`.
2. Gere o executável:

   ```
   pip install -r requirements.txt pyinstaller
   pyinstaller AgenteVarredura.spec --noconfirm
   ```

3. Publique a release com a tag correspondente (`v1.2.0`) anexando o arquivo
   **com o nome exato `AgenteVarredura.exe`** — é por esse nome que o
   auto-update procura o binário.
4. Envie o código junto. Uma release sem o fonte correspondente já custou caro:
   a v1.0.0 ficou quatro meses atrás do código e ninguém tinha como perceber.

## Componentes

| Arquivo | Papel |
|---|---|
| `agente.py` | processo principal, autenticação, auto-update, ícone na bandeja |
| `scan_documentos.py` | varredura e classificação dos documentos do servidor |
| `scraper_agenda.py` | agenda tributária federal e estadual (contadores.cnt.br) |
| `servidor_api.py` | API local em `localhost:5123` para o explorador de arquivos do RNX |
