# Ручной откат, smoke-тесты и GitHub status

## Применение

1. Проверить diff. Изменения этой задачи локальные; commit/push выполняет владелец.
2. После публикации в `main` выполнить полный sync `urban-assistant-bootstrap`:
   это обновляет ApplicationSet, подписки GitHub и sync options дочерних apps.
3. У приложений появятся новые PostSync Jobs. Автосинхронизация включена:
   публикация манифестов может запустить sync без дополнительной команды,
   включая существующие PreSync миграции. Выбрать подходящее время публикации.
4. Просмотреть результат операций и Jobs. `PostSync` запускается после того,
   как обычные ресурсы приложения стали Healthy. Ошибка Job делает sync Failed,
   но **не возвращает автоматически предыдущий образ**.

## Ручной rollback

Открыть **Actions → Roll back dev images → Run workflow**:

1. Ветка workflow: **main**.
2. `service`: имя сервиса из списка.
3. `revision`: полный SHA коммита **urban-assistant-deploy**, в котором у этого
   сервиса были нужные рабочие образы. Не SHA приложения, не digest и не `main~1`.
4. `reason`: короткая причина отката.

Workflow на `47_runner` читает исторический overlay как данные, проверяет
попадание коммита в историю текущего main, текущий allowlist репозиториев,
полноту набора образов и их существование в registry. Исторические scripts/
workflows не выполняются. Создаётся PR; автоматическое слияние не включается.

В PR возвращаются только digest и аннотации происхождения релиза выбранного
сервиса. Ресурсы/patches/ConfigMap остальных и этого сервиса остаются текущими.
Для frontend возвращается и provenance build-time конфигурации старого образа,
но текущий файл сборочной конфигурации в Git не откатывается.

Перед ручным merge:

- подтвердить, что выбранные образы действительно были рабочими (история Git
  сама по себе не доказывает успешный деплой);
- проверить совместимость со **сегодняшними** ConfigMap, Vault и схемой БД;
- согласованно вернуть все образы релиза: API+migrator для Urban API,
  agents+mcp для gMART;
- проверить ожидающие promotion PR и временно не пушить новые релизы этого
  сервиса: автоматическая блокировка продвижений в этой версии не добавлена.

После merge Argo сам применит rollback и выполнит hooks. Миграции снова
запускаются: старый migrator может не понимать новую схему БД. Ни Vault, ни
данные, ни схема БД не откатываются автоматически. При несовместимости нужен
отдельный согласованный план/исправление новой версии, а не принудительный sync.

Если выбранные digest уже стоят — workflow завершится с сообщением об отсутствии
изменений. Импорт `legacy-import` без исходного SHA/workflow не является
полноценным релизом и отвергается. Обычная защита promotion от устаревшего dev
HEAD не изменена; только ручной rollback намеренно разрешает старый source SHA.

Новые GitHub Secrets не нужны: используются существующие `DEPLOY_BOT_APP_ID` /
`DEPLOY_BOT_PRIVATE_KEY`. Registry используется без авторизации для проверки,
сборки и публикации образов; логин/пароль не требуются.

## Что проверяют smoke-тесты

Контракт находится в `services.yaml` → `smokeChecks`.

| Сервис | Проверки |
|---|---|
| frontend | `/`, HTTP 200 и HTML |
| urban-api | `/health_check/db` |
| urban-mcp | MCP initialize для всех шести тематических endpoints; валидный JSON-RPC result |
| chat-storage | `/ping` |
| genbuilder | `/docs`, Swagger UI |
| genplanner | `/docs`, Swagger UI; `/health` MCP |
| gmart | `/ping` agents; `/health` MCP |
| idu-dvd | `/ping` |
| normgraph | `/ping` |
| object-effects | `/status` |
| pzz-compare | `/readiness` с `status=ready` (БД + Redis); MCP health; метрики обоих workers |
| scenarios-conductor | `/metrics` с Prometheus-маркерами |

Каждый Job использует digest образа своего сервиса. Никаких kubeconfig, токенов
ServiceAccount, Vault или прикладных Secrets в Job нет. Python-проверки используют
только стандартную библиотеку; frontend — Node из frontend image.
HTTP redirect/login, пустой ответ, HTTP error и JSON-RPC error не считаются успехом.
Не вызываются LLM, операции записи в БД или бизнес-задания. MCP initialize
создаёт только краткоживущую протокольную сессию; после проверки запрашивается её закрытие.

Это базовые smoke-тесты, а не end-to-end проверка всех функций. Метрики workers
не доказывают обработку сообщений. У gMART context-worker и PZZ beat нет отдельного
health endpoint; их rollout проверяется Argo, но бизнес-обработка не тестируется.
У PZZ workers также уже есть readiness через Celery ping в Deployment.

Лимит Job — 300 секунд, без повторного запуска всего Job; внутри до 12 попыток
с паузой 5 секунд и ограничением времени сетевых запросов. Общий timeout Job
имеет приоритет. Успешный Job удаляется (`HookSucceeded`), неуспешный остаётся
до следующего полного sync (`BeforeHookCreation`). Это не система хранения логов.

Пример диагностики на мастере:

```bash
argocd app get dev-genplanner
kubectl get jobs -n urban-assistant-dev
kubectl logs job/genplanner-smoke-test -n urban-assistant-dev
```

После успешного sync Job уже может отсутствовать — результат сохранён в Argo
operation resources. Не использовать selective sync, если нужен результат smoke.
Полный ручной sync Urban API/PZZ также повторяет PreSync миграцию.

## Изменение smoke-тестов

Редактировать `services.yaml`, `scripts/smoke-probe.py` или
`scripts/frontend-smoke.mjs`, затем:

```bash
python scripts/generate-smoke-tests.py
python scripts/generate-smoke-tests.py --check
python -m unittest discover -s tests -v
python scripts/validate.py --root .
```

Jobs в `apps/*/base/post-sync-smoke-test.yaml` сгенерированы и хранятся в Git,
поэтому Argo использует обычный Kustomize без plugins/envsubst. CI проверяет,
что генератор и закоммиченные Jobs совпадают; также проверяет имена/порты Service
и что smoke Pod не попадает под Service selector приложения.

## GitHub status

Argo публикует на точном коммите **deploy-репозитория**, отдельно по приложениям:
`argocd/dev-<service>`. Успех требует завершённого smoke hook. Новый workflow
`forward-deployment-status.yml` может дублировать этот статус на исходном коммите
приложения, не меняя результат завершённого build-workflow.
Настройка: [статусы в репозиториях приложений](FORWARD-DEPLOYMENT-STATUS.ru.md).
Не добавлять эти статусы в required checks promotion PR: sync происходит после merge.

ApplicationSet содержит подписки; сам Notifications и GitHub App credential
относятся к общей инфраструктуре и находятся вне этого репозитория:
`idu-hosts-audit/cluster-infra/argocd/github-status/README.ru.md`.
Там описаны создание App, Vault, установка и проверка. Подписки без настроенного
Notifications не блокируют деплой, но статусы отправляться не будут.
