#!/usr/bin/python3
# Deproi — simple automatic deployer.
# https://github.com/rodrigo-pasini/deproi
#
# Copyright (c) 2026 Rodrigo Mendes Pasini
# Released under the MIT License — see the LICENSE file in the repository above.
"""Deproi — the only command the deploy key can run.

Publishes the latest commit of the environment's branch: fast-forwards the
checkout, builds the image, runs the configured hooks (migrations…), recreates
the Compose services, waits for the web service to be healthy and, when it is
not, rolls back to the previous image on its own.

How it is called
  - by GitHub Actions, through an SSH key locked to
    ``restrict,command="sudo -n /usr/local/sbin/deproi-<name>"``;
  - by an operator, by hand: ``sudo /usr/local/sbin/deproi-<name>``.
  It accepts no arguments. ``<name>`` (for example ``myapp-prod``) comes from
  the INSTALLED PATH and selects ``/etc/deproi/<name>.json``, owned by root.
  Nothing from the repository, the SSH client or the environment selects the
  target.

Self-update
  The file versioned in the project (``script_path`` in the config) is
  compared with the installed one on every deploy. When they differ, the new
  version is test-loaded, installed atomically and re-executed, inheriting
  the lock.

Constraints
  Standard library only, Python 3.10 syntax (the server's /usr/bin/python3).

Output
  GitHub Actions workflow commands (``::group::``, ``::error::``) and, at the
  end, the machine-readable lines ``message:`` and ``result:``. Everything
  important also goes to syslog (``journalctl -t deproi-<name>``).

Documentation: https://github.com/rodrigo-pasini/deproi
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Protocol

DEPROI_VERSION = "0.2.0"
# Contract marker: the self-update refuses a new version without it, because
# installing it would switch the self-update off until someone reinstalls by hand.
SELF_UPDATING = True

# --- Fixed server layout ------------------------------------------------------

INSTALL_DIR = "/usr/local/sbin"
CONFIG_DIR = "/etc/deproi"
STATE_ROOT = "/var/lib/deproi"
LOCK_DIR = "/run/lock"
SERVER_PYTHON = "/usr/bin/python3"
DOCKER_ROOT = "/var/lib/docker"
DOCS_URL = "https://github.com/rodrigo-pasini/deproi/blob/main/docs/operations.md"

# Re-execution after a self-update: ``<old>:<new>`` fingerprints and the lock descriptor.
ENV_SWAP = "DEPROI_SWAP"
ENV_LOCK_FD = "DEPROI_LOCK_FD"

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_TAG = re.compile(r"[0-9a-f]{12}")
_SWAP = re.compile(r"(?P<before>[0-9a-f]{12}):(?P<after>[0-9a-f]{12})")
_FD = re.compile(r"[0-9]{1,6}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SERVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_CLOUDFLARE_ZONE = re.compile(r"[0-9a-f]{32}")
_REMOVED_CONTAINER = re.compile(r"^\s*Container\s+(?P<name>\S+)\s+Removed\s*$")

HTTP_TIMEOUT = 10
BETTERSTACK_UPTIME_API = "https://incidents.betterstack.com/api/v2"
BETTERSTACK_ERRORS_API = "https://errors.betterstack.com/api/v1"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
MONITORS_STATE = "monitors.json"
POST_DEPLOY_STATE = "post-deploy.json"

# --- Messages (en, pt-BR) -----------------------------------------------------
#
# Machine-readable output (``result:``, situations, verdicts) is always English.
# Everything meant for a person goes through ``t()`` in the configured language.

MESSAGES: dict = {
    "en": {
        "banner": "Deproi {version} — {name}",
        "no_arguments": "This command accepts no arguments.",
        "needs_root": "The deploy must run as root: use {manual}.",
        "unknown_path": (
            "Unknown installed path ({path}): Deproi only runs as {install_dir}/deproi-<name>. Nothing was done."
        ),
        "config_error": "Invalid configuration in {path}: {problem} — nothing was done.",
        "step_lock": "Lock",
        "step_disk": "Disk space",
        "step_checkout": "Update the code",
        "step_script": "Deploy script",
        "step_definition": "Compose definition",
        "step_build": "Build the image {image}:{commit}",
        "step_healthcheck": "Health check of {service}",
        "step_hooks": "Hooks: {point}",
        "step_dependencies": "Dependencies",
        "dependencies_none": "No third-party services.",
        "dependencies_ready": "Running: {services}.",
        "dependencies_failed": "Third-party services did not start ({services}) — no service of the project was recreated. See the error above.",
        "health_gave_up": "{service} reported unhealthy {count} times in a row: not waiting any longer.",
        "step_protected": "Protected services",
        "step_recreate": "Recreate the services",
        "step_health": "Wait for {service} to be healthy",
        "step_rollback": "Roll back to the previous image",
        "step_cleanup": "Clean up old images",
        "step_time": "({title}: {seconds} s)",
        "lock_inherited": "Lock inherited from the previous execution.",
        "lock_timeout": "Another deploy has been running for more than {minutes} minutes — this one gave up without changing anything.",
        "disk_free": "Free space for Docker: {gb:.1f} GB",
        "disk_low": "The server has only {gb:.1f} GB free for Docker — the deploy stopped without changing anything. Free up space (minimum {minimum} GB) and run it again.",
        "fetch_failed": "Could not fetch {branch} from {remote} — the deploy stopped without changing anything. See the error above and run it again.",
        "git_failed": "Git failed in the server checkout — the deploy stopped without changing anything. See the error above.",
        "wrong_branch": "The server checkout is on branch {current}, not {branch} — the deploy stopped without changing anything. Switch back to {branch} on the server and run it again.",
        "dirty_checkout": "The server checkout has local changes in {files} — the deploy stopped without changing anything. Resolve them on the server and run it again.",
        "and_more": "(and {count} more)",
        "env_file_missing": "{env_file} was not found in {branch} — the deploy stopped without changing anything.",
        "wrong_project": "{env_file} points to project {found}, not {project} — the deploy stopped without changing anything.",
        "diverged": "The server's {branch} has commits that are not on {remote} — the deploy stopped without changing anything. Check git log on the server.",
        "ff_failed": "Git could not fast-forward the server checkout — the deploy stopped without changing anything. See the error above; it is usually an untracked file in the way of a new file.",
        "commit_to_publish": "Commit to publish: {commit}",
        "script_unreadable": "Could not read the deploy script from commit {commit} — the environment stays on the previous version and the installed script did not change. See the error above.",
        "script_updated_continuing": "Deploy script updated from {before} to {after} — this deploy continues with the new version.",
        "script_up_to_date": "Deploy script up to date ({fingerprint}, version {version}).",
        "script_pending": "The deploy script of commit {commit} differs from the one in use ({running} → {new}) — it updates on the next deploy.",
        "script_already_installed": "The deploy script of commit {commit} is already installed, but this process runs another version ({before} → {after}): continuing with the installed one.",
        "script_installing": "The deploy script of commit {commit} differs from the installed one ({before} → {after}): installing it and continuing with the new version.",
        "script_does_not_load": "The deploy script of commit {commit} does not load — the environment stays on the previous version and the installed script did not change. Fix it on {branch}.",
        "script_install_failed": "Could not install the deploy script of commit {commit} — the environment stays on the previous version. See the error above.",
        "script_reexec_failed": "The deploy script was updated to {after}, but could not continue with it — the environment stays on the previous version. Run the deploy again.",
        "script_updated_suffix": " Deploy script updated.",
        "definition_failed": "Could not read the Compose definition of commit {commit} — no service was recreated. See the error above.",
        "definition_services": "Services: {services}",
        "definition_empty": "The Compose definition of commit {commit} has no services for profiles {profiles} — no service was recreated.",
        "foreign_services": "The Compose definition starts services without the suffix {suffix} ({services}) — no service was recreated. Give those services a profile (profiles:) or rename them.",
        "missing_service": "The Compose definition has no service {service} — no service was recreated. Check web_service and protected_services in the configuration.",
        "wrong_image": "Service {service} uses image {found}, but Deproi expects {expected} — no service was recreated. Set image: {image}:${{{variable}}} on the services that are built.",
        "build_failed": "The image of commit {commit} did not build — the environment stays on the previous version. See the error above and fix it on {branch}.",
        "no_healthcheck": "Service {service} has no health check (healthcheck: in compose or HEALTHCHECK in the Dockerfile) — no service was recreated. Without one, Deproi cannot tell a broken version from a healthy one.",
        "healthcheck_ok": "Health check found ({origin}).",
        "no_hooks": "No hooks configured.",
        "hook_running": "Hook {name}: {command}",
        "hook_warn": "Warning: hook {name} failed (exit {code}) — the deploy continues.",
        "hook_failed": "Hook {name} ({point}) failed (exit {code}) — no service was recreated. See the error above.",
        "hook_refused": "Hook {name} ({point}) refused the deploy (exit {code}) — no service was recreated. See the message above.",
        "migrate_failed": "Hook {name} (migrate) failed (exit {code}) — no service was recreated. Migrations applied before the failure stay applied. See the error above.",
        "migrate_deadline": "Migrations went past {minutes} minutes and stopped before {name} — no service was recreated. Migrations already applied stay applied. Apply the rest by hand ({docs}).",
        "protected_none": "No protected services.",
        "protected_stopped": "{service} is stopped: the deploy does not start it (operator decision).",
        "protected_busy": "{service} is busy: it stays on the previous version and updates on the next deploy.",
        "protected_no_answer": "{service} did not answer its busy check: it stays on the previous version. Check it on the server.",
        "protected_idle": "{service} is idle: it joins the recreation.",
        "protected_absent": "{service} has no container yet: it is created.",
        "image_before": "Image in use before the deploy: {image}",
        "none": "(none)",
        "republish": "Same commit already live: the services are force-recreated.",
        "removed_containers": "Removed services (containers): {names}",
        "health_status": "health of {service}: {status}",
        "rolling_back": "Rolling back to image {image}:{previous}",
        "rollback_definition": "Using the Compose definition of {previous}.",
        "rollback_current_definition": "The definition of {previous} is not in the checkout: the rollback uses the current one.",
        "protected_stays_new": "{service} picked up work (or did not answer): it stays on the new version.",
        "no_previous": "Version {commit} did not become healthy and there is no previous version to go back to — the environment is down. Follow plan B: {docs}",
        "same_previous": "Version {commit} did not become healthy and it is the same one that was already live — there is no different previous version, and the environment is down. Follow plan B: {docs}",
        "rolled_back": "Version {commit} did not become healthy — the environment went back to {previous}. Applied migrations were not undone.",
        "rollback_failed": "Version {commit} did not become healthy and going back to {previous} did not work either — the environment is down. Follow plan B: {docs}",
        "cleanup_failed": "Could not list the images; cleanup is left for the next deploy.",
        "unexpected": "Unexpected error at step {step} ({error}) — check the environment and, if needed, follow plan B: {docs}",
        "success": "{name} published: commit {commit} is live{where} ({duration}).",
        "success_deferred": "{name} published: commit {commit}. Busy protected services ({services}) stayed on the previous version; they update on the next deploy.",
        "where": " at {url}",
        "bs_off": "off on this server",
        "bs_leftover": "leftover from a previous deploy — {text}",
        "bs_restored_n": "{count} monitors restored",
        "bs_attention": "ATTENTION — monitors not restored ({failures}); restore them by hand ({docs})",
        "bs_untouched": "monitors untouched (the deploy stopped before recreating)",
        "bs_protected": "{count} monitors protected during recreation",
        "bs_and_restored": " and restored",
        "bs_nothing": "no monitor needed protection",
        "bs_paused": "paused, untouched: {ids}",
        "bs_protect_failed": "failed to protect ({failures}) — the deploy continued; an incident may have been opened",
        "bs_protecting": "Better Stack (confirmation of {seconds} s during recreation): {text}",
        "bs_restored_line": "Better Stack: {count} monitors with their confirmation period restored.",
        "bs_no_monitors": "monitors off (no Uptime token or no monitors)",
        "bs_release_off": "release off (no Errors token, application or environment)",
        "bs_release_background": "release {commit} registered in the background",
        "bs_release_not_live": "no release: the new version is not live",
        "bs_cannot_restore": "no Uptime configuration to restore automatically",
        "cf_off": "purge off",
        "cf_not_live": "no purge: the new version is not live",
        "cf_purged": "cache purged ({hosts})",
        "cf_failed": "purge failed — {reason}",
        "wh_off": "off",
        "wh_skipped": "not sent for {situation}",
        "wh_sent": "sent",
        "wh_failed": "failed — {reason}",
        "post_none": "no previous post-deploy recorded",
        "post_previous": "previous deploy {commit}: {parts}",
        "post_release_ok": "release registered",
        "post_release_off": "release off",
        "post_release_failed": "release failed ({reason})",
        "post_hook_ok": "{name} ok",
        "post_hook_failed": "{name} failed ({reason})",
        "post_skipped": "skipped, {live} is live now",
        "post_background": "post-deploy of {commit} in the background",
        "post_not_live": "no post-deploy: the new version is not live",
        "insecure_file": "{path} must be owned by root and not readable or writable by others",
        "unreadable_file": "{path} is missing or unreadable",
        "incomplete_credentials": "{path} is missing {keys}",
        "timed_out": "took more than {seconds} s and was stopped",
    },
    "pt-BR": {
        "banner": "Deproi {version} — {name}",
        "no_arguments": "Este comando não aceita argumentos.",
        "needs_root": "O deploy precisa rodar como root: use {manual}.",
        "unknown_path": (
            "Caminho instalado desconhecido ({path}): o Deproi só roda como {install_dir}/deproi-<nome>. Nada foi feito."
        ),
        "config_error": "Configuração inválida em {path}: {problem} — nada foi feito.",
        "step_lock": "Trava",
        "step_disk": "Espaço em disco",
        "step_checkout": "Atualizar o código",
        "step_script": "Script de deploy",
        "step_definition": "Definição do Compose",
        "step_build": "Construir a imagem {image}:{commit}",
        "step_healthcheck": "Healthcheck do {service}",
        "step_hooks": "Ganchos: {point}",
        "step_dependencies": "Dependências",
        "dependencies_none": "Nenhum serviço de terceiros.",
        "dependencies_ready": "No ar: {services}.",
        "dependencies_failed": "Os serviços de terceiros não subiram ({services}) — nenhum serviço do projeto foi recriado. Veja o erro acima.",
        "health_gave_up": "{service} ficou unhealthy {count} vezes seguidas: não vale esperar mais.",
        "step_protected": "Serviços protegidos",
        "step_recreate": "Recriar os serviços",
        "step_health": "Esperar o {service} ficar saudável",
        "step_rollback": "Reverter para a imagem anterior",
        "step_cleanup": "Limpar imagens antigas",
        "step_time": "({title}: {seconds} s)",
        "lock_inherited": "Trava herdada da execução anterior.",
        "lock_timeout": "Outro deploy está rodando há mais de {minutes} minutos — este desistiu sem mexer em nada.",
        "disk_free": "Espaço livre para o Docker: {gb:.1f} GB",
        "disk_low": "O servidor tem só {gb:.1f} GB livres para o Docker — o deploy parou sem mexer em nada. Libere espaço (mínimo {minimum} GB) e dispare de novo.",
        "fetch_failed": "Não consegui buscar {branch} em {remote} — o deploy parou sem mexer em nada. Veja o erro acima e dispare de novo.",
        "git_failed": "O git do checkout do servidor falhou — o deploy parou sem mexer em nada. Veja o erro acima.",
        "wrong_branch": "O checkout do servidor está na branch {current}, não na {branch} — o deploy parou sem mexer em nada. Volte para a {branch} no servidor e dispare de novo.",
        "dirty_checkout": "O checkout do servidor tem alteração local em {files} — o deploy parou sem mexer em nada. Resolva no servidor e dispare de novo.",
        "and_more": "(e mais {count})",
        "env_file_missing": "Não encontrei {env_file} na {branch} — o deploy parou sem mexer em nada.",
        "wrong_project": "{env_file} aponta para o projeto {found}, não para {project} — o deploy parou sem mexer em nada.",
        "diverged": "A {branch} do servidor tem commit que não está em {remote} — o deploy parou sem mexer em nada. Confira com git log no servidor.",
        "ff_failed": "O git não conseguiu avançar o checkout do servidor — o deploy parou sem mexer em nada. Veja o erro acima; costuma ser um arquivo não rastreado no caminho de um arquivo novo.",
        "commit_to_publish": "Commit a publicar: {commit}",
        "script_unreadable": "Não consegui ler o script de deploy do commit {commit} — o ambiente continua na versão anterior e o script instalado não mudou. Veja o erro acima.",
        "script_updated_continuing": "Script de deploy atualizado de {before} para {after} — este deploy continua com a versão nova.",
        "script_up_to_date": "Script de deploy em dia ({fingerprint}, versão {version}).",
        "script_pending": "O script de deploy do commit {commit} difere do em uso ({running} → {new}) — atualiza no próximo deploy.",
        "script_already_installed": "O script de deploy do commit {commit} já está instalado, mas este processo roda outra versão ({before} → {after}): continuando com a instalada.",
        "script_installing": "O script de deploy do commit {commit} difere do instalado ({before} → {after}): instalando e continuando com a versão nova.",
        "script_does_not_load": "O script de deploy do commit {commit} não carrega — o ambiente continua na versão anterior e o script instalado não mudou. Corrija na {branch}.",
        "script_install_failed": "Não consegui instalar o script de deploy do commit {commit} — o ambiente continua na versão anterior. Veja o erro acima.",
        "script_reexec_failed": "O script de deploy foi atualizado para {after}, mas não consegui continuar com ele — o ambiente continua na versão anterior. Dispare o deploy de novo.",
        "script_updated_suffix": " Script de deploy atualizado.",
        "definition_failed": "Não consegui ler a definição do Compose do commit {commit} — nenhum serviço foi recriado. Veja o erro acima.",
        "definition_services": "Serviços: {services}",
        "definition_empty": "A definição do Compose do commit {commit} não tem serviços para os perfis {profiles} — nenhum serviço foi recriado.",
        "foreign_services": "A definição do Compose sobe serviços sem o sufixo {suffix} ({services}) — nenhum serviço foi recriado. Dê um perfil (profiles:) a esses serviços ou renomeie-os.",
        "missing_service": "A definição do Compose não tem o serviço {service} — nenhum serviço foi recriado. Confira web_service e protected_services na configuração.",
        "wrong_image": "O serviço {service} usa a imagem {found}, mas o Deproi espera {expected} — nenhum serviço foi recriado. Use image: {image}:${{{variable}}} nos serviços construídos.",
        "build_failed": "A imagem do commit {commit} não construiu — o ambiente continua na versão anterior. Veja o erro acima e corrija na {branch}.",
        "no_healthcheck": "O serviço {service} não tem healthcheck (healthcheck: no compose ou HEALTHCHECK no Dockerfile) — nenhum serviço foi recriado. Sem ele, o Deproi não distingue uma versão quebrada de uma saudável.",
        "healthcheck_ok": "Healthcheck encontrado ({origin}).",
        "no_hooks": "Nenhum gancho configurado.",
        "hook_running": "Gancho {name}: {command}",
        "hook_warn": "Aviso: o gancho {name} falhou (saída {code}) — o deploy segue.",
        "hook_failed": "O gancho {name} ({point}) falhou (saída {code}) — nenhum serviço foi recriado. Veja o erro acima.",
        "hook_refused": "O gancho {name} ({point}) recusou o deploy (saída {code}) — nenhum serviço foi recriado. Veja a mensagem acima.",
        "migrate_failed": "O gancho {name} (migrate) falhou (saída {code}) — nenhum serviço foi recriado. Migrations aplicadas antes da falha continuam aplicadas. Veja o erro acima.",
        "migrate_deadline": "As migrations passaram de {minutes} minutos e pararam antes de {name} — nenhum serviço foi recriado. As já aplicadas continuam aplicadas. Aplique o resto à mão ({docs}).",
        "protected_none": "Nenhum serviço protegido.",
        "protected_stopped": "{service} está parado: o deploy não o sobe (decisão de operador).",
        "protected_busy": "{service} está ocupado: fica na versão anterior e atualiza no próximo deploy.",
        "protected_no_answer": "{service} não respondeu à conferência de ocupação: fica na versão anterior. Confira-o no servidor.",
        "protected_idle": "{service} está livre: entra na recriação.",
        "protected_absent": "{service} ainda não tem contêiner: é criado.",
        "image_before": "Imagem em uso antes do deploy: {image}",
        "none": "(nenhuma)",
        "republish": "Mesmo commit que já está no ar: os serviços são recriados à força.",
        "removed_containers": "Serviços removidos (contêineres): {names}",
        "health_status": "saúde do {service}: {status}",
        "rolling_back": "Voltando para a imagem {image}:{previous}",
        "rollback_definition": "Usando a definição do Compose de {previous}.",
        "rollback_current_definition": "A definição de {previous} não está no checkout: a reversão usa a atual.",
        "protected_stays_new": "{service} pegou trabalho (ou não respondeu): fica na versão nova.",
        "no_previous": "A versão {commit} não ficou saudável e não há versão anterior para onde voltar — o ambiente está fora do ar. Siga o plano B: {docs}",
        "same_previous": "A versão {commit} não ficou saudável e é a mesma que já estava no ar — não há versão anterior diferente, e o ambiente está fora do ar. Siga o plano B: {docs}",
        "rolled_back": "A versão {commit} não ficou saudável — o ambiente voltou para {previous}. Migrations aplicadas não foram desfeitas.",
        "rollback_failed": "A versão {commit} não ficou saudável e a volta para {previous} também não — o ambiente está fora do ar. Siga o plano B: {docs}",
        "cleanup_failed": "Não consegui listar as imagens; a limpeza fica para o próximo deploy.",
        "unexpected": "Erro inesperado no passo {step} ({error}) — confira o ambiente e, se preciso, siga o plano B: {docs}",
        "success": "{name} publicado: commit {commit} no ar{where} ({duration}).",
        "success_deferred": "{name} publicado: commit {commit}. Serviços protegidos ocupados ({services}) ficaram na versão anterior; atualizam no próximo deploy.",
        "where": " em {url}",
        "bs_off": "desligado neste servidor",
        "bs_leftover": "sobra de um deploy anterior — {text}",
        "bs_restored_n": "{count} monitores restaurados",
        "bs_attention": "ATENÇÃO — monitores não restaurados ({failures}); restaure à mão ({docs})",
        "bs_untouched": "monitores intocados (o deploy parou antes da recriação)",
        "bs_protected": "{count} monitores protegidos na recriação",
        "bs_and_restored": " e restaurados",
        "bs_nothing": "nenhum monitor precisou de proteção",
        "bs_paused": "pausados, intocados: {ids}",
        "bs_protect_failed": "falhou ao proteger ({failures}) — o deploy seguiu; um incidente pode ter sido aberto",
        "bs_protecting": "Better Stack (confirmação de {seconds} s na recriação): {text}",
        "bs_restored_line": "Better Stack: {count} monitores com a confirmação restaurada.",
        "bs_no_monitors": "monitores desligados (sem token do Uptime ou sem monitores)",
        "bs_release_off": "release desligada (sem token do Errors, aplicação ou ambiente)",
        "bs_release_background": "release {commit} registrada em segundo plano",
        "bs_release_not_live": "sem release: a versão nova não ficou no ar",
        "bs_cannot_restore": "sem a configuração do Uptime para restaurar sozinho",
        "cf_off": "purga desligada",
        "cf_not_live": "sem purga: a versão nova não ficou no ar",
        "cf_purged": "cache purgado ({hosts})",
        "cf_failed": "purga falhou — {reason}",
        "wh_off": "desligado",
        "wh_skipped": "não enviado para {situation}",
        "wh_sent": "enviado",
        "wh_failed": "falhou — {reason}",
        "post_none": "sem pós-deploy anterior registrado",
        "post_previous": "deploy anterior {commit}: {parts}",
        "post_release_ok": "release registrada",
        "post_release_off": "release desligada",
        "post_release_failed": "release falhou ({reason})",
        "post_hook_ok": "{name} ok",
        "post_hook_failed": "{name} falhou ({reason})",
        "post_skipped": "pulado, {live} está no ar agora",
        "post_background": "pós-deploy de {commit} em segundo plano",
        "post_not_live": "sem pós-deploy: a versão nova não ficou no ar",
        "insecure_file": "{path} precisa ter dono root e não pode ser lido nem escrito por outros",
        "unreadable_file": "{path} não existe ou não pode ser lido",
        "incomplete_credentials": "faltam {keys} em {path}",
        "timed_out": "passou de {seconds} s e foi interrompido",
    },
}

LANGUAGE = "en"


def t(key: str, **values) -> str:
    """The message ``key`` in the configured language (English when missing)."""
    template = MESSAGES.get(LANGUAGE, {}).get(key) or MESSAGES["en"][key]
    return template.format(**values)


# --- Configuration --------------------------------------------------------------
#
# /etc/deproi/<name>.json, owned by root and not writable by others. Unknown
# keys are errors (a typo must not silently switch a protection off). Optional
# keys always have safe defaults, so a self-updated script keeps working with
# an older config file.


class ConfigError(Exception):
    """The configuration is invalid; the message names the key."""


HOOK_POINTS = ("pre_migrate", "migrate", "post_migrate", "post_deploy")
DEFAULT_ON_FAILURE = {"pre_migrate": "fail", "migrate": "fail", "post_migrate": "warn", "post_deploy": "warn"}


@dataclasses.dataclass(frozen=True)
class Hook:
    name: str
    command: tuple
    service: str
    env: tuple = ()
    timeout: int | None = None
    on_failure: str = "fail"


@dataclasses.dataclass(frozen=True)
class ProtectedService:
    service: str
    busy_check: tuple
    quiesce: tuple = ()
    resume: tuple = ()


@dataclasses.dataclass(frozen=True)
class BetterStackSettings:
    credentials_file: str
    monitors: tuple = ()
    grace_period: int = 1200
    errors_application: str = ""
    errors_environment: str = ""


@dataclasses.dataclass(frozen=True)
class CloudflareSettings:
    credentials_file: str
    zone_id: str
    hosts: tuple


@dataclasses.dataclass(frozen=True)
class WebhookSettings:
    url_file: str
    format: str = "json"
    events: tuple = ("success", "failure")


@dataclasses.dataclass(frozen=True)
class Config:
    name: str
    language: str
    checkout_path: str
    checkout_owner: str
    checkout_home: str
    branch: str
    remote: str
    script_path: str
    project: str
    compose_files: tuple
    env_file: str | None
    secrets_file: str | None
    profiles: tuple
    service_suffix: str | None
    web_service: str
    image: str
    tag_variables: tuple
    url: str | None
    docs: str
    lock_timeout: int
    build_timeout: int
    migrations_timeout: int
    health_timeout: int
    hook_timeout: int
    post_deploy_timeout: int
    health_interval: int
    min_free_bytes: int
    low_priority: bool
    pull: bool
    hooks: Mapping[str, tuple]
    protected: tuple
    betterstack: BetterStackSettings | None
    cloudflare: CloudflareSettings | None
    webhook: WebhookSettings | None
    # Derived from the name and the server layout.
    lock_path: str
    post_lock_path: str
    state_dir: str
    syslog_tag: str
    manual_command: str


class _Section:
    """Reads one JSON object, tracking used keys so unknown ones are reported."""

    def __init__(self, data, where: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{where or 'the file'} must be an object")
        self.data = data
        self.where = where
        self.used = {"$schema"} if not where else set()

    def _key(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def get(self, key: str, kind, *, required: bool = False, default=None, nullable: bool = True):
        self.used.add(key)
        if key not in self.data or (self.data[key] is None and nullable and not required):
            if required:
                raise ConfigError(f"{self._key(key)} is required")
            return default
        value = self.data[key]
        if kind is int:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{self._key(key)} must be a positive integer")
        elif kind is str:
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{self._key(key)} must be a non-empty string")
        elif kind is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{self._key(key)} must be true or false")
        elif kind == "strings":
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                raise ConfigError(f"{self._key(key)} must be a non-empty list of strings")
            value = tuple(value)
        elif kind == "list":
            if not isinstance(value, list):
                raise ConfigError(f"{self._key(key)} must be a list")
        elif kind == "object":
            if not isinstance(value, dict):
                raise ConfigError(f"{self._key(key)} must be an object")
        return value

    def section(self, key: str) -> _Section | None:
        value = self.get(key, "object")
        return None if value is None else _Section(value, self._key(key))

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ConfigError(f"unknown key {self._key(unknown[0])}")


def _absolute(value: str, key: str) -> str:
    if not os.path.isabs(value):
        raise ConfigError(f"{key} must be an absolute path")
    return os.path.normpath(value)


def _relative(value: str, key: str) -> str:
    normal = os.path.normpath(value)
    if os.path.isabs(normal) or normal.startswith(".."):
        raise ConfigError(f"{key} must be a path inside the checkout")
    return normal


def _parse_hooks(root: _Section) -> dict:
    hooks = {point: () for point in HOOK_POINTS}
    section = root.section("hooks")
    if section is None:
        return hooks
    web = root.data.get("compose", {}).get("web_service") if isinstance(root.data.get("compose"), dict) else None
    for point in HOOK_POINTS:
        items = section.get(point, "list", default=[])
        parsed = []
        for index, item in enumerate(items):
            where = f"hooks.{point}[{index}]"
            hook = _Section(item, where)
            name = hook.get("name", str, required=True)
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
                raise ConfigError(f"{where}.name must be lowercase letters, digits, - or _")
            command = hook.get("command", "strings", required=True)
            service = hook.get("service", str, default=web)
            env = hook.get("env", "object", default={})
            for key, value in env.items():
                if not _ENV_NAME.fullmatch(key) or not isinstance(value, str):
                    raise ConfigError(f"{where}.env must map variable names to strings")
            timeout = hook.get("timeout", int)
            on_failure = hook.get("on_failure", str, default=DEFAULT_ON_FAILURE[point])
            allowed = ("warn",) if point == "post_deploy" else ("fail", "refuse", "warn")
            if on_failure not in allowed:
                raise ConfigError(f"{where}.on_failure must be one of {', '.join(allowed)}")
            hook.done()
            parsed.append(Hook(name, command, service, tuple(sorted(env.items())), timeout, on_failure))
        names = [h.name for h in parsed]
        if len(set(names)) != len(names):
            raise ConfigError(f"hooks.{point} has repeated names")
        hooks[point] = tuple(parsed)
    section.done()
    return hooks


def _parse_protected(root: _Section) -> tuple:
    items = root.get("protected_services", "list", default=[])
    parsed = []
    for index, item in enumerate(items):
        where = f"protected_services[{index}]"
        section = _Section(item, where)
        parsed.append(
            ProtectedService(
                service=section.get("service", str, required=True),
                busy_check=section.get("busy_check", "strings", required=True),
                quiesce=section.get("quiesce", "strings", default=()),
                resume=section.get("resume", "strings", default=()),
            )
        )
        section.done()
    return tuple(parsed)


def _parse_integrations(root: _Section, url: str | None) -> tuple:
    section = root.section("integrations")
    if section is None:
        return None, None, None
    betterstack = cloudflare = webhook = None
    bs = section.section("betterstack")
    if bs is not None:
        monitors = bs.get("monitors", "strings", default=())
        if not all(m.isdigit() for m in monitors):
            raise ConfigError("integrations.betterstack.monitors must be numeric ids")
        errors = bs.section("errors")
        application = environment = ""
        if errors is not None:
            application = errors.get("application_id", str, required=True)
            if not application.isdigit():
                raise ConfigError("integrations.betterstack.errors.application_id must be a numeric id")
            environment = errors.get("environment", str, required=True)
            errors.done()
        betterstack = BetterStackSettings(
            credentials_file=_absolute(
                bs.get("credentials_file", str, required=True), "integrations.betterstack.credentials_file"
            ),
            monitors=monitors,
            grace_period=bs.get("grace_period", int, default=1200),
            errors_application=application,
            errors_environment=environment,
        )
        bs.done()
    cf = section.section("cloudflare")
    if cf is not None:
        zone = cf.get("zone_id", str, required=True)
        if not _CLOUDFLARE_ZONE.fullmatch(zone):
            raise ConfigError("integrations.cloudflare.zone_id must be a 32-character Cloudflare zone id")
        default_hosts = (urllib.parse.urlsplit(url).hostname,) if url else None
        hosts = cf.get("hosts", "strings", default=default_hosts)
        if not hosts:
            raise ConfigError("integrations.cloudflare.hosts is required when url is not set")
        cloudflare = CloudflareSettings(
            credentials_file=_absolute(
                cf.get("credentials_file", str, required=True), "integrations.cloudflare.credentials_file"
            ),
            zone_id=zone,
            hosts=tuple(hosts),
        )
        cf.done()
    wh = section.section("webhook")
    if wh is not None:
        fmt = wh.get("format", str, default="json")
        if fmt not in ("json", "slack", "discord"):
            raise ConfigError("integrations.webhook.format must be json, slack or discord")
        events = wh.get("events", "strings", default=("success", "failure"))
        if not set(events) <= {"success", "failure"}:
            raise ConfigError("integrations.webhook.events accepts success and failure")
        webhook = WebhookSettings(
            url_file=_absolute(wh.get("url_file", str, required=True), "integrations.webhook.url_file"),
            format=fmt,
            events=tuple(events),
        )
        wh.done()
    section.done()
    return betterstack, cloudflare, webhook


def parse_config(
    data,
    name: str,
    *,
    state_root: str = STATE_ROOT,
    lock_dir: str = LOCK_DIR,
    home_of: Callable[[str], str] | None = None,
) -> Config:
    """Validates the decoded JSON and builds the Config. Raises ConfigError."""
    home_of = home_of or (lambda user: pwd.getpwnam(user).pw_dir)
    root = _Section(data, "")
    version = root.get("config_version", int, default=1)
    if version != 1:
        raise ConfigError(f"config_version {version} is not supported by Deproi {DEPROI_VERSION}")
    if root.get("name", str, required=True) != name:
        raise ConfigError(f"name must be {name!r}, the name of the installed command (deproi-{name})")
    language = root.get("language", str, default="en")
    if language not in MESSAGES:
        raise ConfigError(f"language must be one of {', '.join(MESSAGES)}")

    checkout = root.section("checkout")
    if checkout is None:
        raise ConfigError("checkout is required")
    owner = checkout.get("owner", str, required=True)
    if owner == "root":
        raise ConfigError("checkout.owner must not be root (git runs as the checkout owner)")
    try:
        home = home_of(owner)
    except KeyError:
        raise ConfigError(f"checkout.owner {owner!r} does not exist on this server") from None
    checkout_path = _absolute(checkout.get("path", str, required=True), "checkout.path")
    branch = checkout.get("branch", str, required=True)
    remote = checkout.get("remote", str, default="origin")
    checkout.done()

    compose = root.section("compose")
    if compose is None:
        raise ConfigError("compose is required")
    project = compose.get("project", str, required=True)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ConfigError("compose.project must be lowercase letters, digits, - or _")
    files = tuple(_relative(f, "compose.files") for f in compose.get("files", "strings", default=("compose.yaml",)))
    env_file = compose.get("env_file", str)
    env_file = _relative(env_file, "compose.env_file") if env_file else None
    secrets_file = compose.get("secrets_file", str)
    if secrets_file:
        secrets_file = (
            os.path.normpath(secrets_file)
            if os.path.isabs(secrets_file)
            else _relative(secrets_file, "compose.secrets_file")
        )
    profiles = compose.get("profiles", "strings", default=())
    suffix = compose.get("service_suffix", str)
    web_service = compose.get("web_service", str, required=True)
    image = compose.get("image", str, required=True)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", image):
        raise ConfigError("compose.image must be an image repository name without a tag")
    tag_variables = compose.get("tag_variables", "strings", default=("DEPROI_TAG",))
    if not all(_ENV_NAME.fullmatch(v) for v in tag_variables):
        raise ConfigError("compose.tag_variables must be environment variable names")
    compose.done()

    url = root.get("url", str)
    script_path = _relative(root.get("script_path", str, default="deploy/deproi.py"), "script_path")
    docs = root.get("docs", str, default=DOCS_URL)

    timeouts = root.section("timeouts") or _Section({}, "timeouts")
    values = {
        "lock": timeouts.get("lock", int, default=1800),
        "build": timeouts.get("build", int, default=2400),
        "migrations": timeouts.get("migrations", int, default=2400),
        "health": timeouts.get("health", int, default=240),
        "hook": timeouts.get("hook", int, default=600),
        "post_deploy": timeouts.get("post_deploy", int, default=300),
    }
    timeouts.done()
    build = root.section("build") or _Section({}, "build")
    low_priority = build.get("low_priority", bool, default=True)
    pull = build.get("pull", bool, default=False)
    build.done()
    health_interval = root.get("health_interval", int, default=5)
    min_free_gb = root.get("min_free_disk_gb", int, default=5)

    hooks = _parse_hooks(root)
    protected = _parse_protected(root)
    if web_service in {p.service for p in protected}:
        raise ConfigError("the web service cannot be a protected service")
    betterstack, cloudflare, webhook = _parse_integrations(root, url)
    root.done()

    return Config(
        name=name,
        language=language,
        checkout_path=checkout_path,
        checkout_owner=owner,
        checkout_home=home,
        branch=branch,
        remote=remote,
        script_path=script_path,
        project=project,
        compose_files=files,
        env_file=env_file,
        secrets_file=secrets_file,
        profiles=tuple(profiles),
        service_suffix=suffix,
        web_service=web_service,
        image=image,
        tag_variables=tuple(tag_variables),
        url=url,
        docs=docs,
        lock_timeout=values["lock"],
        build_timeout=values["build"],
        migrations_timeout=values["migrations"],
        health_timeout=values["health"],
        hook_timeout=values["hook"],
        post_deploy_timeout=values["post_deploy"],
        health_interval=health_interval,
        min_free_bytes=min_free_gb * 1024**3,
        low_priority=low_priority,
        pull=pull,
        hooks=hooks,
        protected=protected,
        betterstack=betterstack,
        cloudflare=cloudflare,
        webhook=webhook,
        lock_path=os.path.join(lock_dir, f"deproi-{name}.lock"),
        post_lock_path=os.path.join(lock_dir, f"deproi-{name}-post.lock"),
        state_dir=os.path.join(state_root, name),
        syslog_tag=f"deproi-{name}",
        manual_command=f"sudo {INSTALL_DIR}/deproi-{name}",
    )


def check_root_owned(path: str, *, secret: bool = False, stat_fn: Callable = os.stat) -> str | None:
    """None when ``path`` is safe to trust; otherwise the problem, in the configured language.

    Always: owned by root, not writable by group or others. ``secret``: not readable either.
    """
    try:
        info = stat_fn(path)
    except OSError:
        return t("unreadable_file", path=path)
    forbidden = 0o077 if secret else 0o022
    if info.st_uid != 0 or info.st_mode & forbidden or not stat.S_ISREG(info.st_mode):
        return t("insecure_file", path=path)
    return None


def load_config(
    name: str,
    *,
    config_dir: str = CONFIG_DIR,
    state_root: str = STATE_ROOT,
    lock_dir: str = LOCK_DIR,
    stat_fn: Callable = os.stat,
    home_of: Callable[[str], str] | None = None,
) -> Config:
    path = os.path.join(config_dir, f"{name}.json")
    problem = check_root_owned(path, stat_fn=stat_fn)
    if problem:
        raise ConfigError(problem)
    try:
        directory = stat_fn(config_dir)
    except OSError:
        raise ConfigError(t("unreadable_file", path=config_dir)) from None
    if directory.st_uid != 0 or directory.st_mode & 0o022:
        raise ConfigError(t("insecure_file", path=config_dir))
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as failure:
        raise ConfigError(f"{type(failure).__name__}: {failure}") from None
    except ValueError as failure:
        raise ConfigError(f"not valid JSON ({failure})") from None
    return parse_config(data, name, state_root=state_root, lock_dir=lock_dir, home_of=home_of)


def peek_language(name: str, config_dir: str = CONFIG_DIR) -> str:
    """Best effort, before validation: the language for the config error message itself."""
    try:
        with open(os.path.join(config_dir, f"{name}.json"), encoding="utf-8") as handle:
            language = json.load(handle).get("language")
    except Exception:  # noqa: BLE001 — any failure means "use English"
        return "en"
    return language if language in MESSAGES else "en"


# --- Situations and exit codes -------------------------------------------------


class Situation(enum.Enum):
    SUCCESS = "SUCCESS"
    SUCCESS_PROTECTED_DEFERRED = "SUCCESS_PROTECTED_DEFERRED"
    REFUSED = "REFUSED"
    FAILED_NO_CHANGE = "FAILED_NO_CHANGE"
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
    FAILED_ROLLBACK = "FAILED_ROLLBACK"

    @property
    def code(self) -> int:
        return EXIT_CODES[self]

    @property
    def live(self) -> bool:
        """The new version is serving."""
        return self in (Situation.SUCCESS, Situation.SUCCESS_PROTECTED_DEFERRED)


EXIT_CODES = {
    Situation.SUCCESS: 0,
    Situation.SUCCESS_PROTECTED_DEFERRED: 0,
    Situation.REFUSED: 2,
    Situation.FAILED_NO_CHANGE: 3,
    Situation.FAILED_ROLLED_BACK: 4,
    Situation.FAILED_ROLLBACK: 5,
}


@dataclasses.dataclass
class Deploy:
    """One execution. Nothing is persisted: the record is the log and syslog."""

    start: float = 0.0
    commit: str = ""
    previous: str = ""
    step: str = ""
    situation: Situation | None = None
    message: str = ""
    # service → updated | deferred | stopped | rolled_back
    protected: dict = dataclasses.field(default_factory=dict)
    # up_to_date | updated:<a>:<b> | pending | refused | not_checked
    script: str = "not_checked"
    # Services running this project's image (<image>:<commit>): the ones a republish force-recreates.
    own: list = dataclasses.field(default_factory=list)

    @property
    def deferred(self) -> list:
        return sorted(s for s, state in self.protected.items() if state == "deferred")


class Stop(Exception):
    """Ends the deploy with a situation and a message for the operator."""

    def __init__(self, situation: Situation, message: str):
        super().__init__(message)
        self.situation = situation
        self.message = message


def result_line(deploy: Deploy, duration: float) -> str:
    situation = deploy.situation.value if deploy.situation else "-"
    protected = ",".join(f"{s}:{v}" for s, v in sorted(deploy.protected.items())) or "-"
    return (
        f"result: {situation} commit={deploy.commit or '-'} previous={deploy.previous or '-'} "
        f"duration={int(duration)}s protected={protected} step={deploy.step or '-'} script={deploy.script}"
    )


# --- Command executor ------------------------------------------------------------
#
# Every external command goes through here, as a list and never through a shell.
# The test suite swaps the real executor for a fake one that records the calls.


@dataclasses.dataclass
class Result:
    code: int
    output: str = ""


class Executor(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = False,
        timeout: float | None = None,
        echo: bool = False,
    ) -> Result: ...


class RealExecutor:
    """``subprocess`` without a shell.

    Without ``capture``, the child's output goes through ``write`` line by line
    instead of inheriting stdout: if the SSH connection drops, this process gets
    the broken pipe and carries on without a log; an inherited stdout would kill
    ``docker compose`` with SIGPIPE in the middle of ``up``. With ``capture`` and
    ``echo``, both: live log and the output returned to the caller.
    """

    def run(self, command, *, cwd=None, env=None, capture=False, timeout=None, echo=False):
        environment = {**os.environ, **env} if env else None
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as failure:
            return Result(127, str(failure))
        expired = threading.Event()

        def interrupt():
            expired.set()
            process.kill()

        timer = threading.Timer(timeout, interrupt) if timeout else None
        if timer:
            timer.start()
        lines = []
        try:
            for line in process.stdout:
                if capture:
                    lines.append(line)
                if echo or not capture:
                    write(line.rstrip("\n"))
            process.wait()
        finally:
            if timer:
                timer.cancel()
            process.stdout.close()
        if expired.is_set():
            return Result(124, t("timed_out", seconds=int(timeout)))
        return Result(process.returncode, "".join(lines))


# --- Output: GitHub Actions log and syslog -----------------------------------------
#
# ``::group::``, ``::endgroup::`` and ``::error::`` are workflow commands: the
# runner interprets them even when they arrive through the SSH connection.

_broken_output = False


def write(text: str = "") -> None:
    """Writes to the log; with the connection gone, carries on without a log."""
    global _broken_output
    if _broken_output:
        return
    try:
        print(text, flush=True)
    except (BrokenPipeError, OSError):
        _broken_output = True
        # fd 1 now points to /dev/null, so the interpreter's final flush does not raise again.
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, sys.stdout.fileno())
        os.close(null)


@contextlib.contextmanager
def group(title: str, clock: Callable[[], float] = time.monotonic) -> Iterator[None]:
    # Actions does not time groups: each step's duration is its last line.
    start = clock()
    write(f"::group::{title}")
    try:
        yield
    finally:
        write(t("step_time", title=title, seconds=int(clock() - start)))
        write("::endgroup::")


def error(message: str) -> None:
    write(f"::error::{message}")


def syslog(executor: Executor, tag: str, text: str) -> None:
    executor.run(["logger", "-t", tag, "--", text], capture=True, timeout=30)


# --- Commands ----------------------------------------------------------------------


def git_command(cfg: Config, *args: str) -> list:
    # Git as the checkout owner: as root, git refuses the repository ("dubious
    # ownership") and would leave root-owned files behind.
    return [
        "runuser",
        "-u",
        cfg.checkout_owner,
        "--",
        "env",
        f"HOME={cfg.checkout_home}",
        "git",
        "-C",
        cfg.checkout_path,
        *args,
    ]


def compose_command(cfg: Config, *args: str, definition: str | None = None) -> list:
    """``docker compose`` for this environment's project — always with an explicit ``--project-name``.

    ``definition``: a folder holding the compose files and env file of a previous
    commit (rollback); relative paths in them still resolve in the checkout.
    """
    command = ["docker", "compose", "--project-name", cfg.project]
    if definition is None:
        for path in cfg.compose_files:
            command += ["-f", path]
        if cfg.env_file:
            command += ["--env-file", cfg.env_file]
    else:
        command += ["--project-directory", cfg.checkout_path]
        for index, path in enumerate(cfg.compose_files):
            command += ["-f", os.path.join(definition, f"{index}-{os.path.basename(path)}")]
        if cfg.env_file:
            command += ["--env-file", os.path.join(definition, "env")]
    if cfg.secrets_file:
        command += ["--env-file", cfg.secrets_file]
    for profile in cfg.profiles:
        command += ["--profile", profile]
    return [*command, *args]


def tag_env(cfg: Config, tag: str) -> dict:
    # The process environment wins over any value in the env file during
    # Compose interpolation: the same tag for the image and the telemetry.
    return {variable: tag for variable in cfg.tag_variables}


def hook_command(cfg: Config, hook: Hook, container_name: str | None = None) -> list:
    # ``run --rm`` goes through the image entrypoint; ``--no-deps``: dependencies are already up.
    env = [part for key, value in hook.env for part in ("-e", f"{key}={value}")]
    name = ["--name", container_name] if container_name else []
    return compose_command(cfg, "run", "--rm", *name, "--no-deps", *env, hook.service, *hook.command)


def env_values(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def container_ids(cfg: Config, executor: Executor, service: str) -> list | None:
    """Containers of ``service`` in this project (one-off ``run`` containers excluded); None on error."""
    result = executor.run(
        [
            "docker",
            "ps",
            "-a",
            "-q",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={cfg.project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "label=com.docker.compose.oneoff=False",
        ],
        capture=True,
        timeout=60,
    )
    if result.code != 0:
        return None
    return [line.strip() for line in result.output.splitlines() if re.fullmatch(r"[0-9a-f]{12,64}", line.strip())]


# --- Steps -------------------------------------------------------------------------


def free_space(path: str = DOCKER_ROOT) -> int:
    """Free bytes where Docker keeps images (or at the root, if it does not exist)."""
    return shutil.disk_usage(path if os.path.isdir(path) else "/").free


def check_disk(cfg: Config, free: Callable[[], int]) -> None:
    available = free()
    gb = available / 1024**3
    write(t("disk_free", gb=gb))
    if available < cfg.min_free_bytes:
        raise Stop(Situation.FAILED_NO_CHANGE, t("disk_low", gb=gb, minimum=cfg.min_free_bytes // 1024**3))


def _git_failure(result: Result) -> str:
    write(result.output.rstrip())
    return t("git_failed")


def check_project(cfg: Config, executor: Executor) -> None:
    """The env file of the fetched commit must still point to this project.

    Read with ``git show`` BEFORE the checkout moves: refusing here changes nothing.
    """
    if not cfg.env_file:
        return
    read = executor.run(git_command(cfg, "show", f"{cfg.remote}/{cfg.branch}:{cfg.env_file}"), capture=True, timeout=60)
    if read.code != 0:
        raise Stop(Situation.REFUSED, t("env_file_missing", env_file=cfg.env_file, branch=cfg.branch))
    found = env_values(read.output).get("COMPOSE_PROJECT_NAME", "")
    if found != cfg.project:
        raise Stop(
            Situation.REFUSED, t("wrong_project", env_file=cfg.env_file, found=found or "-", project=cfg.project)
        )


def update_checkout(cfg: Config, executor: Executor, deploy: Deploy) -> None:
    """fetch → branch and tracked files checked → merge --ff-only.

    Nothing is discarded: no ``reset``, ``clean`` or ``stash``. Untracked files
    (secrets, ``.env``, backups) stay as they are.
    """
    if executor.run(git_command(cfg, "fetch", cfg.remote, cfg.branch), timeout=300).code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, t("fetch_failed", branch=cfg.branch, remote=cfg.remote))

    branch = executor.run(git_command(cfg, "rev-parse", "--abbrev-ref", "HEAD"), capture=True, timeout=60)
    if branch.code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, _git_failure(branch))
    current = branch.output.strip()
    if current != cfg.branch:
        raise Stop(Situation.REFUSED, t("wrong_branch", current=current, branch=cfg.branch))

    status = executor.run(git_command(cfg, "status", "--porcelain", "--untracked-files=no"), capture=True, timeout=60)
    if status.code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, _git_failure(status))
    changed = [line[3:] for line in status.output.splitlines() if line.strip()]
    if changed:
        files = ", ".join(changed[:3]) + (" " + t("and_more", count=len(changed) - 3) if len(changed) > 3 else "")
        raise Stop(Situation.REFUSED, t("dirty_checkout", files=files))

    check_project(cfg, executor)

    ancestor = executor.run(
        git_command(cfg, "merge-base", "--is-ancestor", "HEAD", f"{cfg.remote}/{cfg.branch}"),
        capture=True,
        timeout=60,
    )
    if ancestor.code == 1:
        raise Stop(Situation.REFUSED, t("diverged", branch=cfg.branch, remote=cfg.remote))
    if ancestor.code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, _git_failure(ancestor))

    merge = executor.run(
        git_command(cfg, "merge", "--ff-only", f"{cfg.remote}/{cfg.branch}"), capture=True, timeout=120
    )
    write(merge.output.rstrip())
    if merge.code != 0:
        raise Stop(Situation.REFUSED, t("ff_failed"))

    commit = executor.run(git_command(cfg, "rev-parse", "--short=12", "HEAD"), capture=True, timeout=60)
    if commit.code != 0 or not _TAG.fullmatch(commit.output.strip()):
        raise Stop(Situation.FAILED_NO_CHANGE, _git_failure(commit))
    deploy.commit = commit.output.strip()
    write(t("commit_to_publish", commit=deploy.commit))


# --- Self-update from the commit ------------------------------------------------------
#
# Compares the running script with the one in the published commit. When they
# differ, checks that the new version loads, installs it at the same path
# (root, 0755, atomic rename) and re-executes it, inheriting the lock. One swap
# per execution: the re-executed process only records what it finds.

# Runs the new version only up to module level (``main`` is guarded by
# ``__main__``) and requires the two contract markers. ``-I -B``: no script
# directory on the import path and no ``__pycache__`` next to the installed file.
LOADER = """\
import importlib.machinery, importlib.util, sys
name = "deproi_new_version"
loader = importlib.machinery.SourceFileLoader(name, sys.argv[1])
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
sys.modules[name] = module
try:
    loader.exec_module(module)
except SystemExit:
    sys.exit("the new version exits while loading")
if not callable(getattr(module, "main", None)):
    sys.exit("the new version has no main function")
if getattr(module, "SELF_UPDATING", None) is not True:
    sys.exit("the new version lacks SELF_UPDATING = True: installing it would switch the self-update off")
"""


def fingerprint(content: bytes) -> str:
    """The first 12 hex digits of the sha256 — the script's identity in the log."""
    return hashlib.sha256(content).hexdigest()[:12]


def running_code() -> bytes:
    """The bytes of the file this process loaded (empty if unreadable: counts as different)."""
    try:
        with open(os.path.abspath(__file__), "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def git_blob_id(content: bytes) -> str:
    """The id git gives a file with these bytes (``git hash-object``)."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324 — git's own id


def read_swap(environ: Mapping[str, str]) -> tuple | None:
    """(before, after) when this process is the re-execution of a swap."""
    match = _SWAP.fullmatch(environ.get(ENV_SWAP, ""))
    return (match["before"], match["after"]) if match else None


def adopt_lock(environ: Mapping[str, str], path: str) -> int | None:
    """The lock descriptor inherited from the previous execution, or None.

    ``flock`` belongs to the open file description: reopening ``path`` would
    collide with the inherited descriptor's own lock. Only adopts a descriptor
    that points to the lock file (same ``st_dev``/``st_ino``) and whose
    ``flock`` succeeds without waiting.
    """
    text = environ.get(ENV_LOCK_FD, "")
    if not _FD.fullmatch(text):
        return None
    descriptor = int(text)
    try:
        inherited = os.fstat(descriptor)
        expected = os.stat(path)
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, False)
    except OSError:
        return None
    return descriptor


def load_script(path: str, executor: Executor, python: str = SERVER_PYTHON) -> tuple:
    """(loaded, loader output) for the file at ``path``."""
    result = executor.run([python, "-I", "-B", "-c", LOADER, path], capture=True, timeout=120)
    return result.code == 0, result.output


def _fsync_directory(directory: str) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_temporary(content: bytes, temporary: str) -> None:
    """The new file, root 0700, in the installed file's directory (so the rename is atomic)."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(temporary)  # leftover from a swap that died halfway
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def install_script(temporary: str, installed: str, chown: Callable = os.chown) -> None:
    """root:root 0755 and ``rename`` over the installed file: the path never points to half a file."""
    chown(temporary, 0, 0)
    os.chmod(temporary, 0o755)  # noqa: S103 — an executable in /usr/local/sbin, owned by root
    os.replace(temporary, installed)
    with contextlib.suppress(OSError):
        _fsync_directory(os.path.dirname(installed))


def _install_failed(deploy: Deploy, failure: OSError) -> Stop:
    write(f"{type(failure).__name__}: {failure}")
    deploy.script = "refused"
    return Stop(Situation.FAILED_NO_CHANGE, t("script_install_failed", commit=deploy.commit))


def update_script(
    cfg: Config,
    executor: Executor,
    deploy: Deploy,
    swap: tuple | None,
    installed: str,
    load: Callable,
    chown: Callable,
    in_use: bytes,
) -> tuple | None:
    """Settles the script verdict; returns (before, after) when the process must re-execute.

    The content comes from the commit object (``git show``), not the working
    tree. ``in_use`` are the bytes this process loaded when it started: the
    comparison is with them, not with the installed file, which another deploy
    may have replaced while this one waited for the lock.
    """
    target = f"{deploy.commit}:{cfg.script_path}"
    read = executor.run(git_command(cfg, "show", target), capture=True, timeout=60)
    expected = executor.run(git_command(cfg, "rev-parse", target), capture=True, timeout=60)
    new = read.output.encode("utf-8")
    # The captured output merges stderr and goes through text decoding: it is
    # only used if it reproduces the commit's object byte for byte.
    if read.code != 0 or expected.code != 0 or git_blob_id(new) != expected.output.strip():
        write(read.output.rstrip() if read.code != 0 else expected.output.rstrip())
        deploy.script = "refused"
        raise Stop(Situation.FAILED_NO_CHANGE, t("script_unreadable", commit=deploy.commit))
    before, after = fingerprint(in_use), fingerprint(new)

    if before == after:
        if swap:
            deploy.script = f"updated:{swap[0]}:{swap[1]}"
            write(t("script_updated_continuing", before=swap[0], after=swap[1]))
        else:
            deploy.script = "up_to_date"
            write(t("script_up_to_date", fingerprint=before, version=DEPROI_VERSION))
        return None
    if swap:
        # The branch moved between the two fetches: the deploy that push triggered does the swap.
        deploy.script = "pending"
        write(t("script_pending", commit=deploy.commit, running=swap[1], new=after))
        return None

    try:
        with open(installed, "rb") as handle:
            current = handle.read()
    except OSError:
        current = b""
    if current == new:
        write(t("script_already_installed", commit=deploy.commit, before=before, after=after))
        return before, after

    write(t("script_installing", commit=deploy.commit, before=before, after=after))
    temporary = os.path.join(os.path.dirname(installed), f".{os.path.basename(installed)}.new")
    try:
        try:
            write_temporary(new, temporary)
        except OSError as failure:
            raise _install_failed(deploy, failure) from None
        loaded, output = load(temporary, executor)
        if not loaded:
            write(output.rstrip())
            deploy.script = "refused"
            raise Stop(Situation.FAILED_NO_CHANGE, t("script_does_not_load", commit=deploy.commit, branch=cfg.branch))
        try:
            install_script(temporary, installed, chown)
        except OSError as failure:
            raise _install_failed(deploy, failure) from None
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)
    return before, after


def reexecute(program: str, args: list, environ: Mapping[str, str]) -> None:
    # Same PID, stdout and stderr: GitHub keeps reading the same stream.
    os.execve(program, args, environ)  # noqa: S606 — re-executes the installed script, no shell


# --- Compose definition ------------------------------------------------------------


def read_definition(cfg: Config, executor: Executor, tag: str, definition: str | None = None) -> dict | None:
    """``compose config --format json`` for this project, interpolated with ``tag``; None on failure.

    The output can contain secrets interpolated from the env files: it is parsed, never printed.
    """
    result = executor.run(
        compose_command(cfg, "config", "--format", "json", definition=definition),
        cwd=cfg.checkout_path,
        env=tag_env(cfg, tag),
        capture=True,
        timeout=120,
    )
    if result.code != 0:
        # Only the lines that are not JSON: Compose's error, without the definition.
        write("\n".join(line for line in result.output.splitlines() if not line.lstrip().startswith(("{", '"'))))
        return None
    start = result.output.find("{")
    try:
        data, _ = json.JSONDecoder().raw_decode(result.output[start:]) if start >= 0 else (None, 0)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return None
    return data


def definition_services(data: dict) -> list:
    return sorted(name for name in data["services"] if _SERVICE_NAME.fullmatch(str(name)))


def check_definition(cfg: Config, executor: Executor, deploy: Deploy) -> list:
    """The fence before anything is built or touched; returns the services of the definition.

    - every service carries the environment suffix (a service without
      ``profiles:`` would otherwise start in every environment's project);
    - the web service and each protected service exist;
    - every built service is tagged ``<image>:<commit>`` (the rollback and the
      cleanup find versions by that tag).
    """
    data = read_definition(cfg, executor, deploy.commit)
    if data is None:
        raise Stop(Situation.FAILED_NO_CHANGE, t("definition_failed", commit=deploy.commit))
    services = definition_services(data)
    if not services:
        raise Stop(
            Situation.REFUSED, t("definition_empty", commit=deploy.commit, profiles=", ".join(cfg.profiles) or "-")
        )
    write(t("definition_services", services=", ".join(services)))
    if cfg.service_suffix:
        foreign = [name for name in services if not name.endswith(cfg.service_suffix)]
        if foreign:
            raise Stop(Situation.REFUSED, t("foreign_services", suffix=cfg.service_suffix, services=", ".join(foreign)))
    for name in [cfg.web_service, *(p.service for p in cfg.protected)]:
        if name not in services:
            raise Stop(Situation.REFUSED, t("missing_service", service=name))
    expected = f"{cfg.image}:{deploy.commit}"
    for name in services:
        service = data["services"][name] or {}
        if name == cfg.web_service or service.get("build"):
            found = str(service.get("image") or "")
            if found != expected:
                raise Stop(
                    Situation.REFUSED,
                    t(
                        "wrong_image",
                        service=name,
                        found=found or "-",
                        expected=expected,
                        image=cfg.image,
                        variable=cfg.tag_variables[0],
                    ),
                )
    deploy.own = [name for name in services if str((data["services"][name] or {}).get("image") or "") == expected]
    return services


def build_image(cfg: Config, executor: Executor, deploy: Deploy) -> None:
    command = compose_command(cfg, "build", *(["--pull"] if cfg.pull else []))
    if cfg.low_priority:
        # The server is usually the one serving traffic.
        command = ["nice", "-n", "10", "ionice", "-c3", *command]
    result = executor.run(command, cwd=cfg.checkout_path, env=tag_env(cfg, deploy.commit), timeout=cfg.build_timeout)
    if result.code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, t("build_failed", commit=deploy.commit, branch=cfg.branch))


def _active_healthcheck(value) -> bool:
    if not isinstance(value, dict) or value.get("disable"):
        return False
    test = value.get("test") or value.get("Test")
    return bool(test) and test != ["NONE"]


def check_healthcheck(cfg: Config, executor: Executor, deploy: Deploy) -> None:
    """The web service must have a health check, from Compose or from the built image."""
    data = read_definition(cfg, executor, deploy.commit)
    service = ((data or {}).get("services") or {}).get(cfg.web_service) or {}
    if _active_healthcheck(service.get("healthcheck")):
        write(t("healthcheck_ok", origin="compose"))
        return
    if service.get("healthcheck", {}).get("disable") if isinstance(service.get("healthcheck"), dict) else False:
        raise Stop(Situation.REFUSED, t("no_healthcheck", service=cfg.web_service))
    image = executor.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Healthcheck}}", f"{cfg.image}:{deploy.commit}"],
        capture=True,
        timeout=60,
    )
    try:
        from_image = json.loads(image.output.strip() or "null") if image.code == 0 else None
    except ValueError:
        from_image = None
    if _active_healthcheck(from_image):
        write(t("healthcheck_ok", origin="Dockerfile HEALTHCHECK"))
        return
    raise Stop(Situation.REFUSED, t("no_healthcheck", service=cfg.web_service))


# --- Dependencies ------------------------------------------------------------------


def start_dependencies(cfg: Config, executor: Executor, deploy: Deploy, services: Sequence[str]) -> None:
    """Third-party services (a database, a cache) must be up before the hooks run on the new image.

    ``up -d --no-recreate --wait``: creates what does not exist yet (the first
    deploy, a new service) and waits for it to be running and healthy; leaves
    running containers alone. They are updated with the rest in "recreate".
    """
    protected = {p.service for p in cfg.protected}
    dependencies = [name for name in services if name not in deploy.own and name not in protected]
    if not dependencies:
        write(t("dependencies_none"))
        return
    result = executor.run(
        compose_command(cfg, "up", "-d", "--no-recreate", "--wait", *dependencies),
        cwd=cfg.checkout_path,
        env=tag_env(cfg, deploy.commit),
        capture=True,
        echo=True,
        timeout=cfg.hook_timeout,
    )
    if result.code != 0:
        raise Stop(Situation.FAILED_NO_CHANGE, t("dependencies_failed", services=", ".join(dependencies)))
    write(t("dependencies_ready", services=", ".join(dependencies)))


# --- Hooks -------------------------------------------------------------------------


def run_hooks(
    cfg: Config,
    executor: Executor,
    deploy: Deploy,
    point: str,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    hooks = cfg.hooks.get(point, ())
    if not hooks:
        write(t("no_hooks"))
        return
    # All migrate hooks share one deadline.
    deadline = clock() + cfg.migrations_timeout if point == "migrate" else None
    for hook in hooks:
        timeout = hook.timeout or cfg.hook_timeout
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise Stop(
                    Situation.FAILED_NO_CHANGE,
                    t("migrate_deadline", minutes=cfg.migrations_timeout // 60, name=hook.name, docs=cfg.docs),
                )
            timeout = min(hook.timeout, remaining) if hook.timeout else remaining
        write(t("hook_running", name=hook.name, command=" ".join(hook.command)))
        result = executor.run(
            hook_command(cfg, hook),
            cwd=cfg.checkout_path,
            env=tag_env(cfg, deploy.commit),
            timeout=timeout,
        )
        if result.code == 0:
            continue
        if hook.on_failure == "warn":
            write(t("hook_warn", name=hook.name, code=result.code))
            continue
        if hook.on_failure == "refuse":
            raise Stop(Situation.REFUSED, t("hook_refused", name=hook.name, point=point, code=result.code))
        key = "migrate_failed" if point == "migrate" else "hook_failed"
        raise Stop(Situation.FAILED_NO_CHANGE, t(key, name=hook.name, point=point, code=result.code))


# --- Protected services ----------------------------------------------------------------
#
# A service that must not be recreated in the middle of its work (a queue
# worker running a long job). Stopped → left alone (operator decision).
# Busy, or no answer → stays on the previous version. Idle → optionally
# quiesced (stops taking new work), checked again, then recreated.


def _exec(executor: Executor, container: str, command: Sequence[str]) -> Result:
    return executor.run(["docker", "exec", container, *command], capture=True, timeout=60)


def _running(executor: Executor, container: str) -> bool:
    result = executor.run(["docker", "inspect", "--format", "{{.State.Running}}", container], capture=True, timeout=60)
    return result.code == 0 and result.output.strip() == "true"


def _busy(executor: Executor, container: str, service: ProtectedService) -> str:
    code = _exec(executor, container, service.busy_check).code
    return {0: "idle", 1: "busy"}.get(code, "no_answer")


def prepare_protected(cfg: Config, executor: Executor, service: ProtectedService) -> tuple:
    """(state, container): absent | stopped | busy | no_answer | idle. ``idle`` may have been quiesced.

    ``absent``: no container yet (first deploy, or a new service) — it is created.
    ``stopped``: a container exists but is not running — someone stopped it; left alone.
    """
    ids = container_ids(cfg, executor, service.service)
    if ids is None:
        return "no_answer", ""
    if not ids:
        return "absent", ""
    running = [cid for cid in ids if _running(executor, cid)]
    if not running:
        return "stopped", ""
    container = running[0]
    state = _busy(executor, container, service)
    if state != "idle" or not service.quiesce:
        return state, container
    if _exec(executor, container, service.quiesce).code != 0:
        resume_protected(executor, container, service)
        return "no_answer", container
    again = _busy(executor, container, service)
    if again != "idle":
        # Picked up work in between: give the work back and defer.
        resume_protected(executor, container, service)
    return again, container


def resume_protected(executor: Executor, container: str, service: ProtectedService) -> None:
    if container and service.resume:
        _exec(executor, container, service.resume)


def prepare_all_protected(cfg: Config, executor: Executor, deploy: Deploy) -> list:
    """Returns [(service, container)] of the idle ones that join the recreation."""
    if not cfg.protected:
        write(t("protected_none"))
        return []
    joining = []
    for service in cfg.protected:
        state, container = prepare_protected(cfg, executor, service)
        if state == "stopped":
            deploy.protected[service.service] = "stopped"
            write(t("protected_stopped", service=service.service))
        elif state == "busy":
            deploy.protected[service.service] = "deferred"
            write(t("protected_busy", service=service.service))
        elif state == "no_answer":
            deploy.protected[service.service] = "deferred"
            write(t("protected_no_answer", service=service.service))
        elif state == "absent":
            joining.append((service, container))
            write(t("protected_absent", service=service.service))
        else:
            joining.append((service, container))
            write(t("protected_idle", service=service.service))
    return joining


# --- Recreate, health, rollback, cleanup ------------------------------------------------


def image_in_use(cfg: Config, executor: Executor) -> str:
    """The tag of the image the web service runs, or empty (first deploy)."""
    ids = container_ids(cfg, executor, cfg.web_service) or []
    if not ids:
        return ""
    result = executor.run(["docker", "inspect", "--format", "{{.Config.Image}}", ids[0]], capture=True, timeout=60)
    image = result.output.strip()
    if result.code != 0 or not image.startswith(f"{cfg.image}:"):
        return ""
    tag = image.split(":", 1)[1]
    return tag if _TAG.fullmatch(tag) else ""


def removed_containers(output: str) -> list:
    names = []
    for line in output.splitlines():
        match = _REMOVED_CONTAINER.match(line)
        if match and match["name"] not in names:
            names.append(match["name"])
    return names


def recreate_services(
    cfg: Config,
    executor: Executor,
    tag: str,
    services: Sequence[str],
    joining: Sequence[str],
    force: bool = False,
    definition: str | None = None,
    own: Sequence[str] = (),
) -> bool:
    """``up -d --remove-orphans`` of the definition.

    Without protected services, the whole profile and no list: a new service
    joins, a removed one becomes an orphan and is removed. With protected
    services, every other service by name, and the idle protected ones after.
    ``force``: republishing the same commit has the same tag, and ``up`` alone
    would recreate nothing — so the services of ``own`` (this project's image)
    are force-recreated afterwards. Third-party services (a database, a cache)
    are never force-recreated.
    """
    env = tag_env(cfg, tag)
    up = ["up", "-d"]
    protected = {p.service for p in cfg.protected}
    targets = [name for name in services if name not in protected] if protected else []
    if protected and not targets:
        return False
    result = executor.run(
        compose_command(cfg, *up, "--remove-orphans", *targets, definition=definition),
        cwd=cfg.checkout_path,
        env=env,
        capture=True,
        echo=True,
        timeout=1200,
    )
    removed = removed_containers(result.output)
    if removed:
        write(t("removed_containers", names=", ".join(removed)))
    ok = result.code == 0
    forced = [name for name in own if name in (targets or services)]
    if ok and force and forced:
        ok = (
            executor.run(
                compose_command(cfg, *up, "--force-recreate", "--no-deps", *forced, definition=definition),
                cwd=cfg.checkout_path,
                env=env,
                capture=True,
                echo=True,
                timeout=1200,
            ).code
            == 0
        )
    if ok and joining:
        ok = (
            executor.run(
                compose_command(cfg, *up, *(["--force-recreate"] if force else []), *joining, definition=definition),
                cwd=cfg.checkout_path,
                env=env,
                timeout=1200,
            ).code
            == 0
        )
    return ok


def web_health(cfg: Config, executor: Executor) -> str:
    ids = container_ids(cfg, executor, cfg.web_service)
    if not ids:
        return "unavailable"
    statuses = []
    for cid in ids:
        result = executor.run(
            ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", cid],
            capture=True,
            timeout=60,
        )
        statuses.append(result.output.strip() if result.code == 0 else "unavailable")
    unhealthy = [s for s in statuses if s != "healthy"]
    return unhealthy[0] if unhealthy else "healthy"


UNHEALTHY_IN_A_ROW = 3


def wait_healthy(cfg: Config, executor: Executor, clock: Callable[[], float], sleep: Callable[[float], None]) -> bool:
    """True once every web container is healthy; False at the deadline.

    Docker only reports ``unhealthy`` after the health check failed its
    retries; seeing it ``UNHEALTHY_IN_A_ROW`` times in a row ends the wait
    early instead of burning the whole timeout before the rollback.
    """
    deadline = clock() + cfg.health_timeout
    unhealthy = 0
    while True:
        status = web_health(cfg, executor)
        write(t("health_status", service=cfg.web_service, status=status))
        if status == "healthy":
            return True
        unhealthy = unhealthy + 1 if status == "unhealthy" else 0
        if unhealthy >= UNHEALTHY_IN_A_ROW:
            write(t("health_gave_up", service=cfg.web_service, count=UNHEALTHY_IN_A_ROW))
            return False
        if clock() >= deadline:
            return False
        sleep(cfg.health_interval)


def clean_images(cfg: Config, executor: Executor, deploy: Deploy) -> None:
    """Removes old images of this environment's repository created by Deproi.

    Only 12-hex tags (the format only Deproi uses). The current one, the
    previous one and any image used by a container stay. A failure here never
    fails the deploy: the new version is already live.
    """
    tags = executor.run(["docker", "image", "ls", cfg.image, "--format", "{{.Tag}}"], capture=True, timeout=120)
    used = executor.run(["docker", "ps", "-a", "--format", "{{.Image}}"], capture=True, timeout=120)
    if tags.code != 0 or used.code != 0:
        write(t("cleanup_failed"))
        return
    keep = {deploy.commit, deploy.previous}
    in_use = {line.strip() for line in used.output.splitlines()}
    for tag in sorted({line.strip() for line in tags.output.splitlines()}):
        if tag in keep or f"{cfg.image}:{tag}" in in_use or not _TAG.fullmatch(tag):
            continue
        executor.run(["docker", "image", "rm", f"{cfg.image}:{tag}"], timeout=300)
    executor.run(["docker", "builder", "prune", "-f", "--filter", "until=168h"], timeout=900)


def save_definition(cfg: Config, executor: Executor, commit: str, folder: str) -> bool:
    """Writes the compose files and env file of ``commit`` into ``folder``.

    False when the commit is not in the checkout, or when its env file does not
    point to this project.
    """
    files = {f"{i}-{os.path.basename(path)}": path for i, path in enumerate(cfg.compose_files)}
    if cfg.env_file:
        files["env"] = cfg.env_file
    contents = {}
    for name, path in files.items():
        read = executor.run(git_command(cfg, "show", f"{commit}:{path}"), capture=True, timeout=60)
        if read.code != 0:
            return False
        contents[name] = read.output
    if cfg.env_file and env_values(contents["env"]).get("COMPOSE_PROJECT_NAME", "") != cfg.project:
        return False
    for name, content in contents.items():
        descriptor = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    return True


def roll_back(
    cfg: Config,
    executor: Executor,
    deploy: Deploy,
    joined: Sequence[tuple],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    if not deploy.previous:
        raise Stop(Situation.FAILED_NO_CHANGE, t("no_previous", commit=deploy.commit, docs=cfg.docs))
    if deploy.previous == deploy.commit:
        raise Stop(Situation.FAILED_ROLLBACK, t("same_previous", commit=deploy.commit, docs=cfg.docs))
    write(t("rolling_back", image=cfg.image, previous=deploy.previous))
    # A recreated protected service may have picked up work while health was checked.
    joining = []
    for service, _ in joined:
        state, container = prepare_protected(cfg, executor, service)
        if state in ("idle", "absent"):
            joining.append((service, container))
        else:
            # Stays on the new version: its state remains "updated".
            write(t("protected_stays_new", service=service.service))
    # The previous definition comes back with the image: a failure caused by the
    # compose file (command, environment, probe) does not repeat in the rollback.
    folder = tempfile.mkdtemp(prefix=f"{cfg.syslog_tag}-")
    try:
        if save_definition(cfg, executor, deploy.previous, folder):
            write(t("rollback_definition", previous=deploy.previous))
            used: str | None = folder
        else:
            write(t("rollback_current_definition", previous=deploy.previous))
            used = None
        data = read_definition(cfg, executor, deploy.previous, used)
        services = definition_services(data) if data else []
        recreated = bool(services) and recreate_services(
            cfg,
            executor,
            deploy.previous,
            services,
            [s.service for s, _ in joining],
            definition=used,
        )
        for service, _ in joining:
            deploy.protected[service.service] = "rolled_back" if recreated else "updated"
        if not recreated:
            for service, container in joining:
                resume_protected(executor, container, service)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if recreated and wait_healthy(cfg, executor, clock, sleep):
        raise Stop(Situation.FAILED_ROLLED_BACK, t("rolled_back", commit=deploy.commit, previous=deploy.previous))
    raise Stop(
        Situation.FAILED_ROLLBACK, t("rollback_failed", commit=deploy.commit, previous=deploy.previous, docs=cfg.docs)
    )


# --- Integrations ------------------------------------------------------------------
#
# Nothing here changes the deploy outcome: every failure becomes summary text
# and a syslog entry. Tokens live in root-only files (never in the JSON) and
# are never printed: every outgoing text goes through ``redact``.


def read_credentials(path: str, keys: Sequence[str], stat_fn: Callable = os.stat) -> tuple:
    """(values, problem): the env-format file, root 0600. ``problem`` is None when every key is set."""
    problem = check_root_owned(path, secret=True, stat_fn=stat_fn)
    if problem:
        return {}, problem
    try:
        with open(path, encoding="utf-8") as handle:
            values = env_values(handle.read())
    except (OSError, UnicodeDecodeError):
        return {}, t("unreadable_file", path=path)
    missing = [key for key in keys if not values.get(key)]
    if missing:
        return values, t("incomplete_credentials", path=path, keys=", ".join(missing))
    return values, None


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[secret]")
    return text


class IntegrationError(Exception):
    """A call to an external service failed; the message carries no secret."""


def http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body=None,
    opener: Callable | None = None,
    secrets: Sequence[str] = (),
) -> dict:
    """One JSON request over ``urllib`` with a timeout. ``opener`` is ``urlopen`` (swapped in tests)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": f"deproi/{DEPROI_VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    if not url.startswith("https://"):
        raise IntegrationError(f"{method}: only https URLs are allowed")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 — https only
    # When the URL itself is the secret (webhooks), only its host is ever shown.
    parts = urllib.parse.urlsplit(url)
    path = parts.netloc if url in secrets else parts.path
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as failure:
        raise IntegrationError(f"{method} {redact(path, secrets)}: HTTP {failure.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as failure:
        raise IntegrationError(redact(f"{method} {path}: {type(failure).__name__} {failure}", secrets)[:200]) from None
    if not raw:
        return {}
    try:
        result = json.loads(raw.decode("utf-8"))
    except ValueError:
        if raw.strip().lower() in (b"ok", b""):
            return {}
        raise IntegrationError(f"{method} {redact(path, secrets)}: unreadable response") from None
    return result if isinstance(result, dict) else {}


def _reason(failure: BaseException, secrets: Sequence[str]) -> str:
    text = str(failure) if isinstance(failure, IntegrationError) else f"{type(failure).__name__}: {failure}"
    return redact(text, secrets)[:200]


# --- Better Stack: monitors get a grace period only while services are recreated ------


class BetterStackClient:
    """The calls the deploy makes. Circuit breaker: the first connection failure
    or timeout switches the client off for the rest of the deploy, so an outage
    costs the job one call, not all of them. An HTTP error does not: the service answered."""

    def __init__(
        self, settings: BetterStackSettings, uptime_token: str, errors_token: str, opener: Callable | None = None
    ):
        self.settings = settings
        self.uptime_token = uptime_token
        self.errors_token = errors_token
        self.opener = opener
        self.down = ""

    @property
    def secrets(self) -> tuple:
        return (self.uptime_token, self.errors_token)

    def _call(self, method: str, url: str, token: str, body=None) -> dict:
        if self.down:
            raise IntegrationError(f"{method}: not attempted ({self.down})")
        try:
            return http_json(method, url, token=token, body=body, opener=self.opener, secrets=self.secrets)
        except IntegrationError as failure:
            if "HTTP " not in str(failure) and "unreadable" not in str(failure):
                self.down = "Better Stack did not answer before"
            raise

    def monitor(self, monitor_id: str) -> dict:
        data = self._call("GET", f"{BETTERSTACK_UPTIME_API}/monitors/{monitor_id}", self.uptime_token)
        attributes = (data.get("data") or {}).get("attributes")
        if not isinstance(attributes, dict):
            raise IntegrationError(f"GET monitor {monitor_id}: response without attributes")
        return attributes

    def update_monitor(self, monitor_id: str, fields: dict) -> None:
        self._call("PATCH", f"{BETTERSTACK_UPTIME_API}/monitors/{monitor_id}", self.uptime_token, fields)

    def releases(self, version: str) -> list:
        query = urllib.parse.urlencode(
            {
                "application_id": self.settings.errors_application,
                "version": version,
                "environment": self.settings.errors_environment,
            }
        )
        data = self._call("GET", f"{BETTERSTACK_ERRORS_API}/releases?{query}", self.errors_token)
        items = data.get("data")
        return items if isinstance(items, list) else []

    def register_release(self, version: str) -> None:
        data = self._call(
            "POST",
            f"{BETTERSTACK_ERRORS_API}/releases",
            self.errors_token,
            {
                "version": version,
                "applications": [int(self.settings.errors_application)],
                "environment": self.settings.errors_environment,
            },
        )
        # An empty ``releases`` means nothing was registered, not success.
        if not data.get("releases"):
            raise IntegrationError("POST /releases: nothing registered (empty releases)")


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = f"{path}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass
class Protection:
    widened: list = dataclasses.field(default_factory=list)
    paused: list = dataclasses.field(default_factory=list)
    failures: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Restoration:
    had_state: bool = False
    restored: list = dataclasses.field(default_factory=list)
    failures: list = dataclasses.field(default_factory=list)


def _paused(attributes: dict) -> bool:
    return bool(attributes.get("paused") or attributes.get("paused_at") or attributes.get("status") == "paused")


def widen_monitors(client, settings: BetterStackSettings, state: str, commit: str) -> Protection:
    """Sets the grace period on active monitors, saving the original value first.

    The state is written BEFORE any change: a deploy that dies halfway leaves
    the next one something to restore. Paused monitors are not touched.
    """
    protection = Protection()
    originals = {}
    # Values already in the state are the real originals: the monitor still has the grace period.
    saved = (read_json(state) or {}).get("monitors")
    saved = saved if isinstance(saved, dict) else {}
    for monitor_id in settings.monitors:
        if monitor_id in saved:
            continue
        try:
            attributes = client.monitor(monitor_id)
        except Exception as failure:  # noqa: BLE001 — a vendor never brings the deploy down
            protection.failures.append(f"{monitor_id}: {_reason(failure, client.secrets)}")
            continue
        if _paused(attributes):
            protection.paused.append(monitor_id)
            continue
        current = attributes.get("confirmation_period")
        if not isinstance(current, int) or isinstance(current, bool):
            protection.failures.append(f"{monitor_id}: response without confirmation_period")
            continue
        if current < settings.grace_period:
            originals[monitor_id] = current
    if not originals:
        return protection
    try:
        write_json(
            state,
            {
                "commit": commit,
                "at": _now(),
                "monitors": {**saved, **{mid: {"confirmation_period": v} for mid, v in originals.items()}},
            },
        )
    except OSError as failure:
        # Nowhere to keep the original value: change nothing.
        protection.failures.append(f"state not written ({type(failure).__name__})")
        return protection
    for monitor_id in originals:
        try:
            client.update_monitor(monitor_id, {"confirmation_period": settings.grace_period})
            protection.widened.append(monitor_id)
        except Exception as failure:  # noqa: BLE001
            protection.failures.append(f"{monitor_id}: {_reason(failure, client.secrets)}")
    return protection


def restore_monitors(client, state: str) -> Restoration:
    """Puts the saved confirmation period back; the state keeps only what did not go back."""
    restoration = Restoration()
    data = read_json(state)
    if data is None:
        return restoration
    restoration.had_state = True
    monitors = data.get("monitors") if isinstance(data.get("monitors"), dict) else {}
    leftover = {}
    for monitor_id, value in monitors.items():
        try:
            client.update_monitor(monitor_id, {"confirmation_period": int(value["confirmation_period"])})
            restoration.restored.append(monitor_id)
        except Exception as failure:  # noqa: BLE001
            restoration.failures.append(f"{monitor_id}: {_reason(failure, client.secrets)}")
            leftover[monitor_id] = value
    with contextlib.suppress(OSError):
        if leftover:
            write_json(state, {**data, "monitors": leftover})
        else:
            os.remove(state)
    return restoration


def register_release(client, settings: BetterStackSettings, version: str) -> dict:
    """GET before POST: republishing or re-running does not duplicate."""
    try:
        for release in client.releases(version):
            attributes = (release or {}).get("attributes") or {}
            if attributes.get("version") == version and settings.errors_environment in (
                attributes.get("environments") or []
            ):
                return {"ok": True, "existed": True}
        client.register_release(version)
    except Exception as failure:  # noqa: BLE001
        return {"ok": False, "reason": _reason(failure, client.secrets)}
    return {"ok": True, "existed": False}


# --- Cloudflare: purge the environment's hosts after a successful deploy ------------------


def purge_cache(settings: CloudflareSettings, token: str, opener: Callable | None = None) -> str:
    try:
        data = http_json(
            "POST",
            f"{CLOUDFLARE_API}/zones/{settings.zone_id}/purge_cache",
            token=token,
            body={"hosts": list(settings.hosts)},
            opener=opener,
            secrets=(token,),
        )
    except IntegrationError as failure:
        return t("cf_failed", reason=_reason(failure, (token,)))
    if data.get("success") is not True:
        details = "; ".join(
            f"{e.get('code')}: {e.get('message')}" for e in (data.get("errors") or []) if isinstance(e, dict)
        )
        return redact(t("cf_failed", reason="success:false" + (f" ({details})" if details else "")), (token,))
    return t("cf_purged", hosts=", ".join(settings.hosts))


# --- Webhook: one message at the end (Slack, Discord or plain JSON) -----------------------


def webhook_payload(cfg: Config, deploy: Deploy, duration: float) -> dict:
    situation = deploy.situation.value if deploy.situation else "-"
    icon = "✅" if deploy.situation and deploy.situation.live else "❌"
    text = f"{icon} {cfg.name}: {deploy.message}"
    if cfg.webhook.format == "slack":
        return {"text": text}
    if cfg.webhook.format == "discord":
        return {"content": text[:2000]}
    return {
        "name": cfg.name,
        "situation": situation,
        "commit": deploy.commit,
        "previous": deploy.previous,
        "step": deploy.step,
        "message": deploy.message,
        "url": cfg.url,
        "duration": int(duration),
        "protected": deploy.protected,
        "deproi_version": DEPROI_VERSION,
    }


def send_webhook(
    cfg: Config, deploy: Deploy, duration: float, *, opener: Callable | None = None, stat_fn: Callable = os.stat
) -> str:
    """The ``webhook:`` summary line. Never raises."""
    if cfg.webhook is None:
        return f"webhook: {t('wh_off')}"
    event = "success" if deploy.situation and deploy.situation.live else "failure"
    if event not in cfg.webhook.events:
        situation = deploy.situation.value if deploy.situation else "-"
        return f"webhook: {t('wh_skipped', situation=situation)}"
    values, problem = read_credentials(cfg.webhook.url_file, ("WEBHOOK_URL",), stat_fn)
    url = values.get("WEBHOOK_URL", "")
    if problem:
        return f"webhook: {t('wh_failed', reason=problem)}"
    try:
        http_json("POST", url, body=webhook_payload(cfg, deploy, duration), opener=opener, secrets=(url,))
    except Exception as failure:  # noqa: BLE001
        return redact(f"webhook: {t('wh_failed', reason=_reason(failure, (url,)))}", (url,))
    return f"webhook: {t('wh_sent')}"


# --- Post-deploy: release and post_deploy hooks, detached from the job ---------------------


def run_post_hook(cfg: Config, executor: Executor, hook: Hook, version: str) -> dict:
    container = f"{cfg.syslog_tag}-post-{hook.name}"
    remove = ["docker", "rm", "-f", container]
    # A leftover (a post-deploy that died) does not block the name; the lock guarantees nobody else uses it.
    executor.run(remove, capture=True, timeout=60)
    timeout = hook.timeout or cfg.post_deploy_timeout
    result = executor.run(
        hook_command(cfg, hook, container_name=container),
        cwd=cfg.checkout_path,
        env=tag_env(cfg, version),
        capture=True,
        timeout=timeout,
    )
    if result.code == 124:
        # Killing the client does not stop the container: it would keep running after the lock is released.
        executor.run(remove, capture=True, timeout=60)
        return {"name": hook.name, "ok": False, "reason": t("timed_out", seconds=int(timeout))}
    if result.code != 0:
        last = next((line.strip() for line in reversed(result.output.splitlines()) if line.strip()), "")
        return {"name": hook.name, "ok": False, "reason": f"exit {result.code}: {last}"[:200]}
    return {"name": hook.name, "ok": True}


def post_deploy(cfg: Config, executor: Executor, client, version: str, record_path: str) -> dict:
    """The detached work. Runs under its own lock; gives up if a newer version went live meanwhile."""
    live = image_in_use(cfg, executor)
    if live and live != version:
        syslog(executor, cfg.syslog_tag, f"post-deploy {version}: skipped, {live} is live now")
        return {"commit": version, "skipped": True, "live": live}
    start = _now()
    settings = cfg.betterstack
    if client is not None and settings is not None and settings.errors_application and client.errors_token:
        release = register_release(client, settings, version)
    else:
        release = {"ok": False, "off": True}
    hooks = []
    for hook in cfg.hooks.get("post_deploy", ()):
        try:
            hooks.append(run_post_hook(cfg, executor, hook, version))
        except Exception as failure:  # noqa: BLE001
            hooks.append({"name": hook.name, "ok": False, "reason": f"{type(failure).__name__}"})
    record = {"commit": version, "start": start, "end": _now(), "release": release, "hooks": hooks}
    try:
        write_json(record_path, record)
    except OSError as failure:
        syslog(
            executor, cfg.syslog_tag, f"post-deploy {version}: could not write {record_path} ({type(failure).__name__})"
        )
    syslog(
        executor,
        cfg.syslog_tag,
        redact(f"post-deploy {version}: {post_deploy_text(record)}", client.secrets if client else ()),
    )
    return record


def post_deploy_text(record: dict | None) -> str:
    if not record:
        return t("post_none")
    commit = record.get("commit") or "?"
    if record.get("skipped"):
        return t("post_previous", commit=commit, parts=t("post_skipped", live=record.get("live") or "?"))
    parts = []
    release = record.get("release") or {}
    if release.get("ok"):
        parts.append(t("post_release_ok"))
    elif not release.get("off"):
        parts.append(t("post_release_failed", reason=release.get("reason") or "?"))
    for hook in record.get("hooks") or []:
        if hook.get("ok"):
            parts.append(t("post_hook_ok", name=hook.get("name")))
        else:
            parts.append(t("post_hook_failed", name=hook.get("name"), reason=hook.get("reason") or "?"))
    return t("post_previous", commit=commit, parts=", ".join(parts) or "-")


class Process:
    """The system calls of the detachment — the test suite swaps it for a fake."""

    def fork(self) -> int:
        return os.fork()

    def wait(self, pid: int) -> None:
        os.waitpid(pid, 0)

    def setsid(self) -> None:
        os.setsid()

    def isolate(self) -> None:
        # stdin/stdout/stderr to /dev/null and everything else closed: SSH only
        # closes when no process holds the channel, and the deploy lock (an
        # inherited ``flock``) is only released when all its descriptors close.
        null = os.open(os.devnull, os.O_RDWR)
        for descriptor in (0, 1, 2):
            os.dup2(null, descriptor)
        os.closerange(3, os.sysconf("SC_OPEN_MAX"))

    def exit(self) -> None:
        os._exit(0)


def acquire_lock(
    path: str,
    wait: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """Exclusive ``flock``; returns the descriptor (the lock lives with the process) or None."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = clock() + wait
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if clock() >= deadline:
                os.close(descriptor)
                return None
            sleep(5)


def detach(
    work: Callable[[], object],
    cfg: Config,
    process: Process | None = None,
    executor: Executor | None = None,
    lock: Callable[[str, float], object] = acquire_lock,
) -> None:
    """Runs ``work`` in a detached grandchild (double fork + ``setsid``); the parent returns at once.

    The deploy exits with its code and the GitHub job ends; the grandchild runs
    under the post-deploy lock and, if another post-deploy holds it, waits.
    """
    process = process or Process()
    pid = process.fork()
    if pid > 0:
        process.wait(pid)
        return
    try:
        process.setsid()
        if process.fork() > 0:
            process.exit()
            return
        process.isolate()
        if (
            lock(cfg.post_lock_path, 2 * cfg.post_deploy_timeout * max(1, len(cfg.hooks.get("post_deploy", ()))))
            is None
        ):
            syslog(
                executor or RealExecutor(),
                cfg.syslog_tag,
                "post-deploy: another post-deploy held the lock for too long; this one does not run",
            )
        else:
            work()
    except BaseException:  # noqa: BLE001, S110 — the grandchild never returns to the deploy flow
        pass
    finally:
        process.exit()


class Integrations:
    """Better Stack, Cloudflare, the webhook and the post-deploy inside one deploy.

    Every call goes through ``_safe``: an exception from here never reaches the deploy.
    """

    def __init__(self, cfg: Config, executor: Executor, *, opener: Callable | None = None, stat_fn: Callable = os.stat):
        self.cfg = cfg
        self.executor = executor
        self.opener = opener
        self.stat_fn = stat_fn
        self.monitors_state = os.path.join(cfg.state_dir, MONITORS_STATE)
        self.post_record = os.path.join(cfg.state_dir, POST_DEPLOY_STATE)
        self.active = False
        self.client: BetterStackClient | None = None
        self.bs_problem = ""
        self.leftover: Restoration | None = None
        self.protection: Protection | None = None
        self.restoration: Restoration | None = None

    @property
    def secrets(self) -> tuple:
        return self.client.secrets if self.client else ()

    def _safe(self, call: Callable, default):
        try:
            return call()
        except Exception as failure:  # noqa: BLE001
            syslog(
                self.executor, self.cfg.syslog_tag, f"integrations: unexpected error ({_reason(failure, self.secrets)})"
            )
            return default

    def _make_client(self) -> BetterStackClient | None:
        settings = self.cfg.betterstack
        if settings is None:
            return None
        values, problem = read_credentials(settings.credentials_file, (), self.stat_fn)
        uptime, errors = values.get("BETTERSTACK_UPTIME_TOKEN", ""), values.get("BETTERSTACK_ERRORS_TOKEN", "")
        if problem or not (uptime or errors):
            self.bs_problem = problem or t(
                "incomplete_credentials",
                path=settings.credentials_file,
                keys="BETTERSTACK_UPTIME_TOKEN / BETTERSTACK_ERRORS_TOKEN",
            )
            return None
        return BetterStackClient(settings, uptime, errors, self.opener)

    @property
    def protects(self) -> bool:
        return bool(self.client and self.client.uptime_token and self.cfg.betterstack.monitors)

    def start(self) -> None:
        """Right after the lock: reads the credentials and restores a dead deploy's leftover."""
        self.active = True
        self.client = self._safe(self._make_client, None)
        if self.client is not None and self.client.uptime_token:
            self.leftover = self._restore()
        elif os.path.exists(self.monitors_state):
            self.leftover = Restoration(had_state=True, failures=[t("bs_cannot_restore")])
        if self.leftover is not None and self.leftover.had_state:
            write(f"Better Stack: {t('bs_leftover', text=self._leftover_text())}")

    def _leftover_text(self) -> str:
        if self.leftover.failures:
            return self._attention(self.leftover)
        return t("bs_restored_n", count=len(self.leftover.restored))

    def _attention(self, restoration: Restoration) -> str:
        return t("bs_attention", failures="; ".join(restoration.failures), docs=self.cfg.docs)

    def _restore(self) -> Restoration:
        return self._safe(
            lambda: restore_monitors(self.client, self.monitors_state),
            Restoration(had_state=True, failures=["unexpected error"]),
        )

    def protect(self, commit: str) -> None:
        if not self.protects:
            return
        self.protection = self._safe(
            lambda: widen_monitors(self.client, self.cfg.betterstack, self.monitors_state, commit),
            Protection(failures=["unexpected error"]),
        )
        write(
            t(
                "bs_protecting",
                seconds=self.cfg.betterstack.grace_period,
                text=self._protection_text(self.protection, None),
            )
        )

    def restore(self) -> None:
        if not self.protects or self.protection is None:
            return
        self.restoration = self._restore()
        if self.restoration.had_state:
            write(t("bs_restored_line", count=len(self.restoration.restored)))

    def _protection_text(self, protection: Protection | None, restoration: Restoration | None) -> str:
        if protection is None:
            return t("bs_untouched")
        parts = []
        if protection.widened:
            restored = restoration is not None and not restoration.failures
            parts.append(t("bs_protected", count=len(protection.widened)) + (t("bs_and_restored") if restored else ""))
        elif not protection.failures:
            parts.append(t("bs_nothing"))
        if protection.paused:
            parts.append(t("bs_paused", ids=", ".join(protection.paused)))
        if protection.failures:
            parts.append(t("bs_protect_failed", failures="; ".join(protection.failures)))
        if restoration is not None and restoration.failures:
            parts.append(self._attention(restoration))
        return "; ".join(parts)

    def summary_lines(self, deploy: Deploy, duration: float) -> list:
        """The ``betterstack:``, ``cloudflare:``, ``webhook:`` and ``post_deploy:`` lines. Never raises."""
        if not self.active:
            return []
        live = bool(deploy.situation and deploy.situation.live)
        lines = [
            self._safe(lambda: self._betterstack_line(deploy, live), "betterstack: ?"),
            self._safe(lambda: self._cloudflare_line(live), "cloudflare: ?"),
            self._safe(
                lambda: send_webhook(self.cfg, deploy, duration, opener=self.opener, stat_fn=self.stat_fn), "webhook: ?"
            ),
            self._safe(lambda: self._post_line(deploy, live), "post_deploy: ?"),
        ]
        return [redact(line, self.secrets)[:400] for line in lines]

    def _betterstack_line(self, deploy: Deploy, live: bool) -> str:
        settings = self.cfg.betterstack
        if settings is None or self.client is None:
            text = t("bs_off") + (f" ({self.bs_problem})" if self.bs_problem else "")
            if self.leftover is not None and self.leftover.had_state:
                text += "; " + t("bs_leftover", text=self._leftover_text())
            return f"betterstack: {text}"
        parts = []
        if self.leftover is not None and self.leftover.had_state:
            parts.append(t("bs_leftover", text=self._leftover_text()))
        parts.append(self._protection_text(self.protection, self.restoration) if self.protects else t("bs_no_monitors"))
        if not (settings.errors_application and self.client.errors_token):
            parts.append(t("bs_release_off"))
        elif live:
            parts.append(t("bs_release_background", commit=deploy.commit))
        else:
            parts.append(t("bs_release_not_live"))
        return "betterstack: " + "; ".join(parts)

    def _cloudflare_line(self, live: bool) -> str:
        settings = self.cfg.cloudflare
        if settings is None:
            return f"cloudflare: {t('cf_off')}"
        if not live:
            return f"cloudflare: {t('cf_not_live')}"
        values, problem = read_credentials(settings.credentials_file, ("CLOUDFLARE_API_TOKEN",), self.stat_fn)
        if problem:
            return f"cloudflare: {t('cf_failed', reason=problem)}"
        token = values["CLOUDFLARE_API_TOKEN"]
        return redact(f"cloudflare: {purge_cache(settings, token, self.opener)}", (token,))

    def _post_line(self, deploy: Deploy, live: bool) -> str:
        previous = post_deploy_text(read_json(self.post_record))
        has_work = bool(self.cfg.hooks.get("post_deploy")) or bool(
            self.cfg.betterstack and self.cfg.betterstack.errors_application
        )
        if not has_work:
            return f"post_deploy: {t('no_hooks')}"
        now = t("post_background", commit=deploy.commit) if live else t("post_not_live")
        return f"post_deploy: {now} ({previous})"

    def work(self, version: str) -> Callable[[], dict]:
        """The post-deploy, with a fresh client: the job's circuit breaker does not apply to it."""

        def run():
            client = self._safe(self._make_client, None)
            return post_deploy(self.cfg, self.executor, client, version, self.post_record)

        return run


# --- Main ------------------------------------------------------------------------------


def main(
    argv: Sequence[str],
    name: str,
    *,
    executor: Executor | None = None,
    environ: Mapping[str, str] | None = None,
    euid: Callable[[], int] = os.geteuid,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    lock: Callable[[str, float], object] = acquire_lock,
    free: Callable[[], int] = free_space,
    config_loader: Callable[[str], Config] | None = None,
    integrations: Callable[[Config, Executor], Integrations] | None = None,
    background: Callable[[Callable[[], object], Config], None] | None = None,
    installed: str | None = None,
    load: Callable[[str, Executor], tuple] | None = None,
    execute: Callable[[str, list, Mapping[str, str]], None] | None = None,
    chown: Callable | None = None,
    in_use: bytes | None = None,
) -> int:
    global LANGUAGE
    executor = executor or RealExecutor()
    environ = os.environ if environ is None else environ
    installed = installed or os.path.join(INSTALL_DIR, f"deproi-{name}")
    load = load or load_script
    execute = execute or reexecute
    chown = chown or os.chown
    # Read before waiting for the lock: this process's code, even if another
    # deploy replaces the installed file meanwhile.
    in_use = running_code() if in_use is None else in_use
    swap = read_swap(environ)
    tag = f"deproi-{name}"
    deploy = Deploy(start=clock())
    if swap:
        deploy.script = f"updated:{swap[0]}:{swap[1]}"
    cfg: Config | None = None
    extras: Integrations | None = None
    services_touched = False

    # Recorded, never executed nor used to decide anything.
    origin = environ.get("SUDO_USER") or environ.get("USER") or "?"
    syslog(
        executor,
        tag,
        f"start version={DEPROI_VERSION} origin={origin} client={environ.get('SSH_CLIENT', '-')!r:.80} "
        f"ssh_original_command={environ.get('SSH_ORIGINAL_COMMAND')!r:.200}"
        + (f" reexecution={swap[0]}:{swap[1]}" if swap else ""),
    )

    try:
        LANGUAGE = peek_language(name)
        write(t("banner", version=DEPROI_VERSION, name=name))
        if argv:
            raise Stop(Situation.REFUSED, t("no_arguments"))
        if euid() != 0:
            raise Stop(Situation.REFUSED, t("needs_root", manual=f"sudo {installed}"))
        try:
            cfg = (config_loader or load_config)(name)
        except ConfigError as problem:
            raise Stop(
                Situation.REFUSED, t("config_error", path=os.path.join(CONFIG_DIR, f"{name}.json"), problem=problem)
            ) from None
        LANGUAGE = cfg.language
        extras = (integrations or (lambda c, e: Integrations(c, e)))(cfg, executor)

        deploy.step = "lock"
        with group(t("step_lock"), clock):
            descriptor = adopt_lock(environ, cfg.lock_path)
            if descriptor is not None:
                write(t("lock_inherited"))
            else:
                descriptor = lock(cfg.lock_path, cfg.lock_timeout)
            if descriptor is None:
                raise Stop(Situation.REFUSED, t("lock_timeout", minutes=cfg.lock_timeout // 60))
            extras.start()

        deploy.step = "disk"
        with group(t("step_disk"), clock):
            check_disk(cfg, free)

        deploy.step = "checkout"
        with group(t("step_checkout"), clock):
            update_checkout(cfg, executor, deploy)

        deploy.step = "script"
        with group(t("step_script"), clock):
            swapped = update_script(cfg, executor, deploy, swap, installed, load, chown, in_use)
        if swapped:
            # Outside the group: the new process opens its own. So far the only
            # effect on the server is the checkout moving forward, which it repeats at no cost.
            os.set_inheritable(descriptor, True)
            try:
                execute(
                    SERVER_PYTHON,
                    [SERVER_PYTHON, installed],
                    {
                        **environ,
                        ENV_SWAP: f"{swapped[0]}:{swapped[1]}",
                        ENV_LOCK_FD: str(descriptor),
                    },
                )
            except OSError as failure:
                write(f"{type(failure).__name__}: {failure}")
            # ``execve`` does not return: getting here means staying on the old version.
            deploy.script = f"updated:{swapped[0]}:{swapped[1]}"
            raise Stop(Situation.FAILED_NO_CHANGE, t("script_reexec_failed", after=swapped[1]))

        deploy.step = "definition"
        with group(t("step_definition"), clock):
            services = check_definition(cfg, executor, deploy)

        deploy.step = "build"
        with group(t("step_build", image=cfg.image, commit=deploy.commit), clock):
            build_image(cfg, executor, deploy)

        deploy.step = "healthcheck"
        with group(t("step_healthcheck", service=cfg.web_service), clock):
            check_healthcheck(cfg, executor, deploy)

        deploy.step = "dependencies"
        with group(t("step_dependencies"), clock):
            start_dependencies(cfg, executor, deploy, services)

        for point in ("pre_migrate", "migrate", "post_migrate"):
            deploy.step = point
            with group(t("step_hooks", point=point), clock):
                run_hooks(cfg, executor, deploy, point, clock)

        deploy.step = "protected"
        with group(t("step_protected"), clock):
            joining = prepare_all_protected(cfg, executor, deploy)

        # From here until health (or rollback) the web service blinks: monitors
        # get a grace period and have it back in the ``finally``.
        try:
            deploy.step = "recreate"
            with group(t("step_recreate"), clock):
                extras.protect(deploy.commit)
                deploy.previous = image_in_use(cfg, executor)
                write(t("image_before", image=deploy.previous or t("none")))
                republish = deploy.previous == deploy.commit
                if republish:
                    write(t("republish"))
                services_touched = True
                recreated = recreate_services(
                    cfg,
                    executor,
                    deploy.commit,
                    services,
                    [s.service for s, _ in joining],
                    force=republish,
                    own=deploy.own,
                )
                if recreated:
                    for service, _ in joining:
                        deploy.protected[service.service] = "updated"
                else:
                    for service, container in joining:
                        # Quiesced but not recreated: back to work.
                        resume_protected(executor, container, service)
                        deploy.protected[service.service] = "deferred"

            deploy.step = "health"
            with group(t("step_health", service=cfg.web_service), clock):
                healthy = recreated and wait_healthy(cfg, executor, clock, sleep)

            if not healthy:
                deploy.step = "rollback"
                with group(t("step_rollback"), clock):
                    roll_back(cfg, executor, deploy, joining if recreated else [], clock, sleep)
        finally:
            extras.restore()

        deploy.step = "cleanup"
        with group(t("step_cleanup"), clock):
            clean_images(cfg, executor, deploy)

        elapsed = clock() - deploy.start
        duration = f"{int(elapsed)} s" if elapsed < 60 else f"{round(elapsed / 60)} min"
        if deploy.deferred:
            deploy.situation = Situation.SUCCESS_PROTECTED_DEFERRED
            deploy.message = t(
                "success_deferred", name=cfg.name, commit=deploy.commit, services=", ".join(deploy.deferred)
            )
        else:
            deploy.situation = Situation.SUCCESS
            where = t("where", url=cfg.url) if cfg.url else ""
            deploy.message = t("success", name=cfg.name, commit=deploy.commit, where=where, duration=duration)
        if deploy.script.startswith("updated:"):
            deploy.message += t("script_updated_suffix")
    except Stop as stop:
        deploy.situation = stop.situation
        deploy.message = stop.message
        error(stop.message)
    except Exception as unexpected:  # noqa: BLE001 — the result line always comes out
        deploy.situation = Situation.FAILED_ROLLBACK if services_touched else Situation.FAILED_NO_CHANGE
        deploy.message = t(
            "unexpected",
            step=deploy.step or "start",
            error=f"{type(unexpected).__name__}: {unexpected}",
            docs=cfg.docs if cfg else DOCS_URL,
        )
        error(deploy.message)
    finally:
        duration = clock() - deploy.start
        if extras is not None:
            try:
                for line in extras.summary_lines(deploy, duration):
                    write(line)
                    syslog(executor, tag, line)
            except Exception as failure:  # noqa: BLE001 — the result line always comes out
                syslog(executor, tag, f"summary lines failed ({type(failure).__name__})")
        result = result_line(deploy, duration)
        write(f"message: {deploy.message}")
        write(result)
        syslog(executor, tag, result)

    if deploy.situation.live and extras is not None and cfg is not None:
        try:
            (background or (lambda work, c: detach(work, c, executor=executor)))(extras.work(deploy.commit), cfg)
        except Exception as failure:  # noqa: BLE001 — the deploy already ended
            syslog(executor, tag, f"post-deploy did not detach ({type(failure).__name__})")
    return deploy.situation.code


def entry(argv: Sequence[str]) -> int:
    """The name comes from the path the script was called by; unknown, refuse without touching anything."""
    path = os.path.abspath(argv[0])
    match = re.fullmatch(r"deproi-(" + NAME_PATTERN.pattern + ")", os.path.basename(path))
    if not match or os.path.dirname(path) != INSTALL_DIR:
        write(f"::error::{t('unknown_path', path=path, install_dir=INSTALL_DIR)}")
        write("result: REFUSED commit=- previous=- duration=0s protected=- step=- script=not_checked")
        return Situation.REFUSED.code
    return main(list(argv[1:]), match[1], installed=path)


if __name__ == "__main__":
    # A dropped connection does not interrupt the deploy: it finishes and records to syslog.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    sys.exit(entry(sys.argv))
