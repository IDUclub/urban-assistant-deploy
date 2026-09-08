# Статусы Argo на исходных коммитах приложений

`forward-deployment-status.yml` дублирует статусы `argocd/dev-<service>` с коммита
`urban-assistant-deploy` на исходный коммит соответствующего приложения.
Первичный статус в deploy-репозитории сохраняется. Настройки Argo Notifications,
Vault, smoke Jobs и workflow сборки в приложениях менять не нужно.

## 1. Расширить установку GitHub App

Использовать **ту же GitHub App, которой Argo уже публикует статусы**.
В настройках её установки в IDUclub добавить нужные репозитории приложений из
`services.yaml`, сохранив доступ к `urban-assistant-deploy`.

Право App: **Commit statuses → Read and write**; Metadata остаётся обязательным.
Не нужны Contents write, Actions write или доступ к Kubernetes.
Для установки в той же организации App ID и Installation ID не меняются.

Можно начать с `ChatStorage`, затем добавить остальные репозитории. Пока у App
нет доступа к конкретному репозиторию, соответствующий forward job завершится
ошибкой выпуска токена; это не отменяет успешный деплой и его исходный статус.

## 2. Добавить два GitHub Secrets

В **urban-assistant-deploy → Settings → Secrets and variables → Actions →
Repository secrets**:

| Secret | Значение |
|---|---|
| `ARGO_STATUS_APP_ID` | App ID той же App, что используется Notifications |
| `ARGO_STATUS_PRIVATE_KEY` | Полный PEM private key этой App с переводами строк |

Не использовать ключ deploy-bot или read-only source-reader вместо status App.
Installation ID отдельным Secret не нужен: action находит установку по owner.
Ожидаемое имя bot определяется из самой App, отдельная переменная не нужна.

Ключ остаётся в Vault для Argo, дополнительно доступен этому workflow через
GitHub Secrets. В репозитории приложений копировать его не нужно. Не включать
ключ в YAML и не печатать его для проверки. Если GitHub Actions ограничены
политикой организации, разрешить используемые actions и hosted runner.

## 3. Опубликовать workflow

Проверить изменения и опубликовать их в `main` deploy-репозитория.
`status` запускает workflow только при наличии файла в default branch.
Ничего копировать на мастер или обновлять через Helm для этого шага не нужно.
Оба job используют `ubuntu-latest`, без registry, kubeconfig и Vault-токена.

События обычного CI и инфраструктурных приложений пропускаются. Для подходящего
события первый job определяет сервис/исходный SHA, второй публикует статус
токеном с правом statuses write **только на один нужный source-репозиторий**.

## 4. Проверить на ChatStorage

Сначала убедиться, что Argo уже публикует статус в deploy-репозиторий. Затем
сделать обычный release ChatStorage либо полный sync уже готового приложения:

```bash
argocd app sync dev-chat-storage
argocd app wait dev-chat-storage --sync --health --operation --timeout 600
```

В deploy-репозитории открыть **Actions → Forward deployment status**. В summary
job `forward` будут исходный repository/SHA, context и перенесённое состояние.
На указанном коммите `IDUclub/ChatStorage` проверить `argocd/dev-chat-storage`.
Details ведёт в Argo, а description содержит SHA deploy-коммита.

Старые статусы не переносятся задним числом автоматически: нужен новый status
event. После исправления Secrets/прав App можно повторить неудавшийся workflow.
Не использовать полный sync Urban API/PZZ для первой проверки без необходимости:
их PreSync hooks повторно запускают миграции.

## Привязка к релизу и запоздавшие события

- Берём **`github.event.sha`**, не `github.sha`: для события status последний
  указывает на default branch, а не обязательно на развёрнутый коммит.
- Читаем `services.yaml` и аннотации overlay именно из deploy-коммита события.
  Исторические scripts/workflows не выполняются. Source repository дополнительно
  проверяется по текущему allowlist; deploy-коммит должен быть в истории main.
- Имя отправителя события и creator актуального статуса проверяются по status App.
  Перед записью повторно читается актуальный upstream status из GitHub API;
  запоздавший pending event может сразу перенести уже наступивший success/failure.
- Записи одного сервиса и source SHA сериализованы. Дубликаты пропускаются;
  более старый deploy-коммит не затирает результат более нового для того же SHA.
  Новая попытка sync того же deploy-коммита может снова стать pending.
- Для Urban API и Urban MCP контексты разные, даже если repository/SHA совпадают.
- При rollback новый deploy-коммит связывается с восстановленным старым source SHA.
  Проверка текущего HEAD ветки dev здесь намеренно не используется.
- `legacy-import` без source SHA пропускается. При изменении только конфигурации
  статус того же source SHA может обновиться; подробная история остаётся в deploy.
- Если контекст source-коммита уже занят чужой App или статусом без relay provenance,
  workflow не перезаписывает его. Не создавать этот же контекст вручную.

Это дополнительные commit statuses, не изменение результата build workflow.
Не добавлять их в required checks перед merge в dev: deploy начинается после merge.
GitHub может объединить ожидающие запуски одного concurrency group: промежуточные
состояния могут не отобразиться, актуальный статус перепроверяется перед записью.

Документация: [status event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#status),
[commit statuses API](https://docs.github.com/en/rest/commits/statuses).
