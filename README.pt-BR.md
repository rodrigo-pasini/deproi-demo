[🇺🇸 English](README.md) · 🇧🇷 Português

# Demo do Deproi

Um projeto pequeno implantado com o **[Deproi](https://github.com/rodrigo-pasini/deproi)** — o deployer simples automático. Faça push na `main`, e o app é reconstruído, migrado, recriado e conferido no servidor dele, com reversão automática.

Use como referência de como um projeto fica depois que o Deproi está no lugar, ou faça um fork para testar o Deproi no seu próprio servidor.

> **Situação:** os deploys estão pausados (`DEPROI_PAUSED=true` no environment `production`) até um servidor ser ligado. Cada push mostra "paused" no Actions e não conecta em nada.

## O que tem aqui

| Arquivo | Papel |
|---|---|
| [`deploy/deproi.py`](deploy/deproi.py) | O próprio Deproi, copiado sem alteração de uma release. Atualizado por pull requests do `deproi-update.yml`. |
| [`.github/workflows/deploy-production.yml`](.github/workflows/deploy-production.yml) | Gerado pelo `render-workflow.sh` do Deproi, sem edição. Ele só abre o SSH; o servidor decide o que roda. |
| [`.github/workflows/deproi-update.yml`](.github/workflows/deproi-update.yml) | Semanal: abre um pull request quando sai uma versão nova do Deproi, depois de conferir o checksum. |
| [`compose.yaml`](compose.yaml) | Perfil `prod`, sufixo `-prod`, `image: deproi-demo:${DEPROI_TAG}`, `init: true`, política de restart. |
| [`Dockerfile`](Dockerfile) | `HEALTHCHECK` em `/health`: é por ele que o Deproi decide entre sucesso e reversão. |
| [`deploy/prod.compose.env`](deploy/prod.compose.env) | `COMPOSE_PROJECT_NAME` e outros valores não secretos. |
| [`docs/deploy/deproi-demo-prod.json`](docs/deploy/deproi-demo-prod.json) | A configuração do servidor, `/etc/deproi/deproi-demo-prod.json`: um gancho `migrate` e um worker protegido. |
| [`app/`](app/) | `server.py` (web, `/health`), `migrate.py` (o gancho de migrate), `worker.py` (um serviço protegido). |

## Como um deploy funciona aqui

1. Um push na `main` roda o **Deploy production**.
2. O job conecta como `deproi@servidor` com uma chave que só consegue rodar `sudo -n /usr/local/sbin/deproi-deproi-demo-prod`.
3. No servidor, o Deproi avança o `/srv/deproi-demo-prod`, constrói `deproi-demo:<commit>`, roda `python migrate.py` na imagem nova, recria o `web-prod` (e o `worker-prod` quando está livre) e espera o healthcheck.
4. Saudável → `result: SUCCESS`. Não saudável → a imagem anterior volta → `result: FAILED_ROLLED_BACK`.

Commite um arquivo vazio `app/BROKEN` para ver uma reversão: o `/health` dessa versão responde 500.

## Teste no seu próprio servidor

1. Faça um **fork** deste repositório.
2. Siga o [guia de instalação](https://github.com/rodrigo-pasini/deproi/blob/main/docs/installation.pt-BR.md) do Deproi com `--name deproi-demo-prod`, checkout `/srv/deproi-demo-prod`, branch `main`, GitHub environment `production` e o [`docs/deploy/deproi-demo-prod.json`](docs/deploy/deproi-demo-prod.json) como configuração (troque `checkout.owner` pelo seu usuário).
3. Apague a variável `DEPROI_PAUSED` do environment `production` e faça push.

O app responde na porta 8080 do servidor: `curl http://<servidor>:8080/`.

**Sem servidor:** num clone do Deproi, ensaie tudo num contêiner local:

```bash
scripts/try-locally.sh --project ../deproi-demo --config ../deproi-demo/docs/deploy/deproi-demo-prod.json
```

## Licença

[MIT](LICENSE).
