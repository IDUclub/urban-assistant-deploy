# urban-assistant-deploy

Конфигурация развёртывания платформы «Помощник проектировщика» в Kubernetes и общий пайплайн доставки её приложений. Репозиторий хранит желаемое состояние платформы: версии образов, настройки, маршруты, интеграцию с Vault и платформенные компоненты.

Исходный код приложений находится в отдельных репозиториях. Разработчик выпускает код через ветку `dev` приложения, а конфигурацию меняет через PR в `main` этого репозитория. Argo CD применяет состояние из Git — копировать файлы на мастер и выполнять `kubectl apply` для обычного релиза не требуется.

Сейчас используется только **dev**. `environments/prod/` — неактивный каркас: production Applications и пайплайна продвижения в prod пока нет.

Это руководство для разработчиков. Установка Kubernetes, Argo CD, runners и настройка хостов в него не входят.

## Содержание

- [Архитектура](#architecture)
- [Структура репозитория](#layout)
- [Сервисы](#services)
- [Автоматический деплой](#delivery)
- [Проверка результата и GitHub status](#status)
- [Изменение конфигурации и секретов](#configuration)
- [Frontend](#frontend)
- [Откат](#rollback)
- [Миграции и smoke-тесты](#checks)
- [Добавление сервиса](#new-service)
- [Проверки и правила PR](#contributing)
- [Частые ситуации](#troubleshooting)

<a id="architecture"></a>
## Архитектура

Dev работает в существующем кластере с одним control-plane и тремя worker-узлами. Основные приложения находятся в namespace `urban-assistant-dev`, Kafka — в `kafka`, мониторинг — в `monitoring`.

```text
Браузер
  └─ Envoy Gateway: HTTPS NodePort 31302 (HTTP 31301 → redirect)
      ├─ /                  → frontend
      ├─ /urban-api         → Urban API
      ├─ /gmart             → gMART
      ├─ /chat-storage      → ChatStorage
      ├─ /pzz-compare       → PZZ Compare
      ├─ /genbuilder        → GenBuilder
      ├─ /genplanner        → GenPlanner
      └─ /idu-dvd           → IDU DVD

Приложения
  ├─ внутренние Kubernetes Services → API, MCP, Redis, Kafka
  ├─ внешние БД и сервисы           → настройки и credentials из Vault
  └─ метрики и трассировки          → Prometheus, OTel Collector, Jaeger

Vault → Vault Secrets Operator → Kubernetes Secrets → приложения
```

Маршруты и преобразования путей заданы в [platform/gateway/routes.yaml](platform/gateway/routes.yaml). Не все backend/MCP-сервисы опубликованы через Gateway: внутренние вызовы используют Kubernetes DNS.

| Технология | Назначение |
|---|---|
| Kubernetes + Kustomize | Deployments, Services, PVC, ConfigMap; общие bases и dev overlays |
| GitHub Actions + Docker Buildx | Тестирование, сборка образов и PR с новыми digest |
| Argo CD + ApplicationSet | Синхронизация Git с кластером; отдельное Application на сервис |
| Helm | Версии платформенных charts в [operators/releases.yaml](operators/releases.yaml), values в `operators/` |
| Vault + Vault Secrets Operator | Значения вне Git, генерация Secret по `VaultStaticSecret` |
| Reloader | Перезапуск настроенных Deployments при изменении ConfigMap; для Secrets используются также `rolloutRestartTargets` VSO |
| Envoy Gateway / Gateway API | HTTPS, маршрутизация и streaming |
| Strimzi / Kafka | Три совмещённых broker/controller в KRaft, topics, Schema Registry и Kafka UI |
| Prometheus stack, OpenTelemetry, Jaeger | Метрики, dashboards и трассировки |
| NFS CSI / PVC | Постоянное хранилище через `urban-assistant-nfs`; сам NFS находится вне Kubernetes |

Kafka использует три PVC по 5 GiB. StorageClass имеет политику `Retain`, для Kafka задано `deleteClaim: false`. Это не заменяет резервные копии: общий NFS остаётся общей точкой отказа для этих томов.

**В этом репозитории:** приложения Urban Assistant, их Kafka/Redis, платформенный мониторинг, маршруты Gateway, Vault-интеграция, namespaces платформы и её StorageClass.

**Вне этого репозитория:** установка кластера и хостов, сам Argo CD и Notifications, общие NFS CSI / Vault Secrets Operator / Envoy Gateway controllers, Metrics Server, Headlamp, внешние БД и Vault. Общие настройки кластера вынесены в `idu-hosts-audit/cluster-infra`; разработчику не нужно менять их для выпуска приложения.

<a id="layout"></a>
## Структура репозитория

| Путь | Что хранится |
|---|---|
| [services.yaml](services.yaml) | Контракт доставки: source repository и `dev`, Dockerfile/context/target, aliases образов, overlay, миграции и smoke-проверки |
| [vault-contract.yaml](vault-contract.yaml) | Все используемые ключи Vault по KV-путям, **без значений** |
| [apps/](apps/) | `<service>/base/`: общие workload-манифесты, probes, Services, ServiceMonitors, hooks |
| [environments/dev/apps/](environments/dev/apps/) | Dev-настройки, ссылки на Vault и точные image digest каждого сервиса |
| [environments/dev/prerequisites/](environments/dev/prerequisites/) | Конфигурация и Secrets, нужные до миграции Urban API/PZZ; для PZZ также Redis и PVC через base |
| [environments/dev/build/frontend.env](environments/dev/build/frontend.env) | Несекретная сборочная конфигурация frontend |
| [platform/](platform/) и [environments/dev/platform/](environments/dev/platform/) | Kafka, Gateway, мониторинг и Vault-интеграция: общие ресурсы и dev-настройки |
| [cluster/](cluster/) и [environments/dev/cluster/](environments/dev/cluster/) | StorageClass и namespaces платформы |
| [operators/](operators/) | Зафиксированные версии Helm charts и values |
| [argocd/root/](argocd/root/) | AppProjects, Applications и ApplicationSets штатного GitOps-режима |
| [argocd/adoption/](argocd/adoption/) | Режим первоначального подключения существующих ресурсов без автоматического sync; не для обычных релизов |
| [scripts/](scripts/) и [tests/](tests/) | Рендер, проверки, promotion, rollback, перенос статусов, генерация smoke Jobs и их тесты |
| [.github/workflows/](.github/workflows/) | Общие workflows доставки и валидации |
| [ci-templates/](ci-templates/) | Шаблон вызывающего workflow для репозитория приложения |

Bases используют логические имена образов; overlay подставляет registry repository и `digest: sha256:…`. Итоговый манифест получается обычным Kustomize, без `local/dev.env`, `envsubst` и секретов на машине разработчика.

<a id="services"></a>
## Сервисы

Имя сервиса используется в workflow (`service`), каталоге и Argo: `dev-<service>`.

| Сервис | Репозиторий приложения | Образы одного релиза | PostSync smoke-проверка |
|---|---|---|---|
| `frontend` | [IDUclub/Urban-Assistant-Client](https://github.com/IDUclub/Urban-Assistant-Client) | `frontend` | `/`: HTTP 200 и HTML |
| `urban-api` | [IDUclub/idu_api](https://github.com/IDUclub/idu_api) | `api`, `migrator` | `/health_check/db` |
| `urban-mcp` | [IDUclub/idu_api](https://github.com/IDUclub/idu_api) | `mcp` | MCP initialize для шести тематических endpoints |
| `chat-storage` | [IDUclub/ChatStorage](https://github.com/IDUclub/ChatStorage) | `api` | `/ping` |
| `genbuilder` | [IDUclub/genbuilder_api](https://github.com/IDUclub/genbuilder_api) | `api` | `/docs`, Swagger UI |
| `genplanner` | [IDUclub/GenPlanner](https://github.com/IDUclub/GenPlanner) | `api` для API и MCP | `/docs` API и `/health` MCP |
| `gmart` | [IDUclub/gMART](https://github.com/IDUclub/gMART) | `agents`, `mcp` | `/ping` agents и `/health` MCP |
| `idu-dvd` | [IDUclub/IDU_DVD](https://github.com/IDUclub/IDU_DVD) | `api` | `/ping` |
| `normgraph` | [IDUclub/NormGraph](https://github.com/IDUclub/NormGraph) | `api` | `/ping` |
| `object-effects` | [IDUclub/ObjectEffectsAPI](https://github.com/IDUclub/ObjectEffectsAPI) | `api` | `/status` |
| `pzz-compare` | [IDUclub/PzzCompareAPI](https://github.com/IDUclub/PzzCompareAPI) | `api` для API, MCP, workers и migrator | `/readiness` (БД + Redis), MCP health, метрики обоих workers |
| `scenarios-conductor` | [IDUclub/scenarios_conductor](https://github.com/IDUclub/scenarios_conductor) | `api` — образ worker | `/metrics` с Prometheus-маркерами |

<a id="delivery"></a>
## Автоматический деплой

```text
PR / push → dev приложения
  → тесты из вызывающего workflow
  → Buildx: сборка всех образов сервиса на 47_runner
  → registry: dev-<полный source SHA>, digest из результата build
  → одно событие promote-dev-image в deploy-репозиторий
  → проверка контракта, актуальности dev HEAD и наличия digest
  → bot PR: атомарное обновление образов и аннотаций происхождения
  → проверки PR → squash auto-merge в main
  → Argo CD: PreSync миграция, если есть → rollout → PostSync smoke
  → GitHub status на deploy-коммите → копия на source-коммите
```

### Выпустить новую версию

1. Подготовить совместимые изменения конфигурации, если они нужны: [порядок ниже](#configuration).
2. В репозитории приложения влить изменения в `dev`. Workflow должен вызывать правильный `service` и выполнять нужные тесты.
3. Дождаться успешных тестов, сборки и отправки события в Actions приложения.
4. В `urban-assistant-deploy` найти bot PR: проверить source SHA, ссылку на исходный workflow и полный набор обновляемых образов. PR автоматически сольётся после выполнения условий защиты `main`.
5. Проверить `argocd/dev-<service>` на коммите или результат операции в Argo. Успешная сборка **ещё не означает** успешный деплой.

Релиз не мгновенный: после сборки есть очередь PR-проверок, слияние, обнаружение Git-изменения Argo, rollout и hooks. Обнаружение может занимать несколько минут; точный интервал зависит от настроек Argo. Не нужно повторять push или запускать sync только из-за небольшой задержки.

### Кто выполняет каждый этап

| Workflow | Запуск | Результат |
|---|---|---|
| [reusable-application-release.yaml](.github/workflows/reusable-application-release.yaml) | Вызов из приложения при push в `dev` | Тесты, сборка, публикация и один dispatch на сервис |
| [promote-dev-image.yaml](.github/workflows/promote-dev-image.yaml) | `repository_dispatch: promote-dev-image` | Bot PR с digest и включённым squash auto-merge |
| [validate.yaml](.github/workflows/validate.yaml) | PR, push в `main`, ручной запуск | Проверки конфигурации и инструментов репозитория |
| [rollback-dev.yml](.github/workflows/rollback-dev.yml) | Ручной запуск | PR с прежними образами; merge вручную |
| [forward-deployment-status.yml](.github/workflows/forward-deployment-status.yml) | Событие GitHub `status` | Перенос результата Argo на source-коммит |

Тесты, валидация и перенос статусов используют GitHub-hosted runners. Сборка, promotion и rollback работают на `47_runner` с доступом к on-prem registry. GitHub Actions не подключается к control-plane и не получает kubeconfig.

Тег `dev-<SHA>` не перезаписывается workflow. Digest берётся из `docker/build-push-action`, а не определяется по изменяемому тегу. HEAD проверяется до build, перед dispatch и при promotion: старые сборки отклоняются. Это не блокировка ветки на время ожидания PR — перед ручным merge или откатом проверяйте также новые ожидающие promotion PR.

### Что Argo обновляет автоматически

У дочерних Applications включены `automated`, `selfHeal`, `prune`, а `allowEmpty` выключен. Изменения workload/config применяются автоматически; ручные правки управляемых live-ресурсов могут быть возвращены к состоянию Git. Удаление ресурса из Git может удалить его из кластера — особенно внимательно проверяйте PVC и namespaces.

Корневое Application `urban-assistant-bootstrap` синхронизируется **вручную**. Если PR меняет определения Applications, AppProjects или шаблоны ApplicationSet в `argocd/root/`, после merge нужен sync корневого приложения владельцем платформы. Обычное обновление image digest этого не требует. ApplicationSet сам читает список каталогов приложений и releases операторов из Git.

<a id="status"></a>
## Проверка результата и GitHub status

### Через GitHub и Argo UI

1. В deploy-репозитории открыть `main` → историю коммитов → **коммит после merge promotion PR**, а не коммит удалённой bot-ветки.
2. Открыть индикатор статусов возле SHA. Нужный контекст — `argocd/dev-<service>`, например `argocd/dev-chat-storage`.
3. `Details` ведёт к Application в Argo. Проверить Git revision, `Synced`, `Healthy` и последнюю операцию `Succeeded`, включая smoke hook.
4. В репозитории приложения открыть историю `dev` и **исходный SHA релиза**. Там появляется такой же контекст; описание содержит deploy SHA.

Это **commit status**, а не результат job сборки: он не меняет уже завершённый workflow приложения. `pending` означает выполняющуюся операцию, `success` — успешный sync с пройденным smoke, `failure` — операцию Argo в состоянии `Failed` или `Error`. Зависший rollout может оставаться `pending`: автоматического отката нет.

Контексты Urban API и Urban MCP разные, даже когда source SHA совпадает. Изменение только конфигурации может обновить статус того же source SHA. При rollback статус ставится на восстановленный старый source-коммит; история всех деплоев остаётся в deploy-репозитории.

Если копии статуса нет, открыть **Actions → Forward deployment status** в deploy-репозитории. Summary job `forward` показывает repository, SHA, context и перенесённое состояние. Старые события не переносятся автоматически задним числом; после исправления доступа можно повторить неудавшийся workflow. `legacy-import` без source SHA пропускается.

Статусы `argocd/*` нельзя делать обязательными проверками **перед merge** релиза: деплой происходит после слияния. Они также не являются непрерывной проверкой всех бизнес-функций после rollout.

<a id="configuration"></a>
## Изменение конфигурации и секретов

### Где что хранится

| Данные | Место |
|---|---|
| Несекретные параметры, внутренние Kubernetes endpoints | ConfigMap / dev overlay в Git |
| Внешние runtime endpoints, пароли, токены, TLS private keys | Vault; в Git только ссылки, ключи и шаблоны `VaultStaticSecret` |
| Состав `config.yaml` / env-файла | ConfigMap либо шаблон `VaultStaticSecret`, в зависимости от наличия секретов |
| Публичные параметры сборки frontend | `environments/dev/build/frontend.env` и выделенные GitHub Secrets |
| GitHub App private keys для CI | GitHub Secrets, не Kubernetes-манифесты |

В Git остаются адрес registry в ссылках на образы, адрес подключения к самому Vault, NFS server/share и upstream Git/Helm URLs. Это параметры доставки и инфраструктуры. Внутрикластерные адреса, например `http://gmart-mcp:8000/mcp`, не нужно переносить в Vault.

`vault-contract.yaml` описывает используемые ключи KV v2 mount `urban-assistant-kv`. Валидатор сверяет их с шаблонами, но **не обращается к живому Vault** и не проверяет значения. Urban API и Urban MCP намеренно используют общий путь `dev/urban-api`.

### Сценарий 1. Изменить несекретную runtime-переменную

1. Найти ConfigMap в `environments/dev/apps/<service>/`. Для PZZ используется `environments/dev/prerequisites/pzz-compare/`.
2. Изменить значение и открыть PR в deploy-репозиторий. Если параметр находится внутри `.env.development`, менять ключ внутри этого файла, а не создавать дублирующее значение в другом месте.
3. Пройти проверки и влить PR в `main`.
4. Дождаться sync и перезапуска при изменении отслеживаемого ConfigMap; проверить статус и нужную функцию приложения.

Пересборка backend-образа не нужна. Для нового ConfigMap проверьте, что workload действительно читает его и что он указан в настройках перезапуска: наличие файла в Git само по себе ничего не подключает.

### Сценарий 2. Добавить ключ Vault перед обновлением кода

1. Убедиться, что новая настройка совместима с **текущей** версией приложения. На время перехода сохранять старые ключи и формат.
2. Подготовить PR: добавить преобразование ключа в `vault-secret.yaml`, при необходимости ссылку в workload, и обновить ключи соответствующего пути в `vault-contract.yaml` по алфавиту.
3. **До merge** добавить значение в существующий Vault KV-путь, сохранив остальные ключи. Не заменять весь объект записью только нового ключа. Если прав нет — запросить изменение у владельца Vault.
4. Влить конфигурационный PR и дождаться готовности `VaultStaticSecret`. Для Urban API/PZZ сначала проверить `dev-urban-api-prerequisites` / `dev-pzz-compare-prerequisites`.
5. Выпустить новый код через `dev` приложения и проверить результат.
6. Удалять старые ключи отдельным изменением после проверки новой версии и окончания нужного периода отката.

Например, ChatStorage читает `mongo_url` из `dev/chat-storage`, а [vault-secret.yaml](environments/dev/apps/chat-storage/vault-secret.yaml) формирует переменную `MONGO_URL`. Контракт перечисляет именно ключи Vault, не имена переменных приложения.

Проверка **без вывода значений** при наличии Kubernetes-доступа:

Изменение Vault или ConfigMap может перезапустить старые Pods через VSO/Reloader **до нового image release**. Раздельные PR и Vault-операции не являются общей транзакцией. Если новый формат ломает старый код, сначала выпускайте совместимую переходную версию; если это невозможно — согласуйте отдельное переключение. Миграционный hook не задерживает изменение конфигурации в другом Application.

### Сценарий 3. Сменить значение существующего секрета

Если имя ключа и шаблон не меняются, обновить значение в Vault, сохранив остальные ключи. Git-изменение или новый backend build не требуется. Дождаться обновления Secret и перезапуска настроенных `rolloutRestartTargets`, затем проверить сервис. Совместимость ротации credentials нужно согласовать с внешним сервисом.

### Файловая конфигурация и `APP_ENV`

У GenBuilder, GenPlanner, ObjectEffects и PZZ настройки включают монтируемый `/app/.env.development`; одного `APP_ENV=development` недостаточно. При изменении схемы проверить имя файла, mountPath/subPath, ConfigMap и требуемые ключи. Файл с несекретными параметрами и переменные из Secret дополняют друг друга.

У Urban API `config.yaml` формируется Vault-шаблоном в [prerequisites/urban-api](environments/dev/prerequisites/urban-api/). Это отдельное Application: Secret нужен migration Job до обновления Deployment.

<a id="frontend"></a>
## Frontend

Frontend собирается под окружение: значения `VITE_*` встраиваются в браузерный bundle. Runtime ConfigMap или повторный sync без нового образа не изменят их.

Сборка берёт [frontend.env](environments/dev/build/frontend.env) из зафиксированного deploy-коммита и через [render-frontend-env.sh](scripts/render-frontend-env.sh) создаёт временный `service.env`. Из GitHub Secrets, доступных workflow приложения, приходят:

| GitHub Secret | Переменная в сборке |
|---|---|
| `MAPBOX_PUBLIC_TOKEN` | `VITE_MAPBOX_TOKEN` |
| `FRONTEND_KEYCLOAK_AUTH_URL` | `VITE_KEYCLOAK_AUTH_URL` |
| `FRONTEND_KEYCLOAK_LOGOUT_REDIRECT` | `VITE_KEYCLOAK_AUTH_LOGOUT_REDIRECT` |

Эти значения становятся **публичными в браузере**, даже если переданы через GitHub Secrets. Здесь нельзя использовать серверные credentials или Keycloak client secret.

Чтобы изменить frontend-настройку:

1. Для нового backend-префикса сначала добавить маршрут в `platform/gateway/routes.yaml` и проверить доступность backend.
2. Через PR обновить `frontend.env`, либо изменить соответствующее значение GitHub Secrets.
3. После подготовки маршрутов выпустить **новый коммит** в `dev` frontend. Повторная сборка того же SHA не перезапишет существующий immutable tag.
4. Проверить promotion PR, `argocd/dev-frontend` и приложение в браузере: авторизацию и изменённые API-вызовы.

Аннотация config revision сохраняет SHA Git-конфигурации, но не версию значений GitHub Secrets. `service.env` в Git не хранится. Для будущего prod frontend потребуется отдельная сборка; backend предполагается продвигать по готовому digest без пересборки.

<a id="rollback"></a>
## Откат

Используется GitOps-откат через PR, не ручная замена image в live Deployment. Автоматического отката при сбое сейчас нет.

1. Найти коммит deploy-репозитория с **проверенными рабочими** образами сервиса и скопировать полный 40-символьный SHA.
2. Открыть **Actions → Roll back dev images → Run workflow**.
3. Выбрать ветку workflow `main`, `service`, указать SHA в `revision` и причину в `reason`. Нужен SHA **urban-assistant-deploy**, не SHA приложения, tag или image digest.
4. Дождаться PR. Workflow проверит коммит в истории `main`, полный набор aliases и наличие всех исторических digest в registry.
5. Проверить совместимость старого образа с текущими ConfigMap, Vault и схемой БД. Проверить ожидающие promotion PR и согласованно приостановить новые релизы сервиса, чтобы следующий auto-merge не отменил откат.
6. Влить rollback PR **вручную** после проверок. Дождаться автоматического sync, hooks и GitHub status.

Возвращаются только образы выбранного сервиса и аннотации их происхождения. Конфигурация, PVC, Vault и БД остаются текущими. Для Urban API откатываются вместе `api` + `migrator`, для gMART — `agents` + `mcp`; frontend возвращается со встроенной в старый образ конфигурацией.

Миграции Urban API/PZZ запускаются снова и **не выполняют автоматический downgrade БД**. Если старый migrator несовместим с новой схемой, нужен согласованный план исправления, а не принудительный sync. `legacy-import` без source SHA и исходного workflow не подходит для этого механизма.

Если сломано только runtime-значение, исправить его отдельным PR / изменением Vault: rollback образов его не восстановит. Не удаляйте старые registry digest, пока они нужны для отката.

<a id="checks"></a>
## Миграции и smoke-тесты

Для Urban API и PZZ порядок одной полной операции Argo:

1. `PreSync`: migration Job ожидает БД и выполняет миграцию с ограничениями времени и попыток.
2. `Sync`: применяются workload-ресурсы; Argo ожидает готовности.
3. `PostSync`: smoke Job проверяет доступность сервиса из кластера.

Для остальных приложений миграционного шага нет. Prerequisites Urban API/PZZ должны быть готовы заранее: отдельные Applications не образуют общей транзакции. Неуспешная миграция блокирует применение новой версии workload в этой операции. Неуспешный PostSync делает операцию неуспешной, но **уже обновлённый Deployment не откатывается**.

Все сервисы имеют smoke Job; проверки перечислены в [таблице сервисов](#services) и `services.yaml → smokeChecks`. Job использует образ приложения по digest, без Vault credentials, kubeconfig или ServiceAccount token. Проверки не вызывают LLM и не создают бизнес-задания; MCP initialize открывает краткоживущую протокольную сессию с запросом закрытия после проверки.

Лимит smoke Job — 300 секунд, `backoffLimit: 0`; внутри есть ограниченные повторы сетевых проверок. Успешный Job удаляется по `HookSucceeded`, неуспешный остаётся до следующего полного sync (`BeforeHookCreation`). Поэтому отсутствие успешного Job при чтении логов нормально: итог хранится в результатах операции Argo.

Smoke не заменяет функциональные тесты: ответ `/docs` не доказывает работу генерации, метрики worker — обработку сообщений. Отдельные gMART context-worker / PZZ beat не имеют HTTP smoke endpoint. После значимого релиза проверяйте изменённый пользовательский сценарий.

Для изменения smoke редактировать `services.yaml` или [smoke-probe.py](scripts/smoke-probe.py) / [frontend-smoke.mjs](scripts/frontend-smoke.mjs), затем:

```bash
python scripts/generate-smoke-tests.py
python scripts/generate-smoke-tests.py --check
```

Сгенерированные `apps/*/base/post-sync-smoke-test.yaml` включаются в тот же PR. Не редактируйте их независимо от источника. Выборочный sync ресурсов не запускает hooks; полный повторный sync Urban API/PZZ повторяет и миграцию.

<a id="new-service"></a>
## Добавление сервиса

1. Подготовить в приложении обязательную ветку `dev`, Dockerfile и тестовую команду, которая устанавливает зависимости и действительно проверяет приложение. Для конфигурации при импорте использовать тестовые файлы/значения, не production secrets.
2. Добавить запись в `services.yaml`: точные repository, `branch: dev`, `argocdPath`, все image aliases и Dockerfile/context/target, `atomicImages`, миграцию при наличии и `smokeChecks`.
3. Добавить `apps/<service>/base/` и `environments/dev/apps/<service>/`. Подключить overlay в [environments/dev/kustomization.yaml](environments/dev/kustomization.yaml); сохранить уникальность имён, selectors и NodePort. Для первого подключения закрепить реально существующие образы по digest — не фиктивный digest или `latest`.
4. Добавить ConfigMap, Vault-шаблоны и ключи `vault-contract.yaml`; заранее подготовить значения Vault. Для новой миграции отдельно согласовать prerequisite Application и порядок первого запуска.
5. Сгенерировать smoke Job, подключить его в base Kustomization; добавить сервис в список выбора `rollback-dev.yml`. При необходимости добавить ServiceMonitor и Gateway route.
6. Открыть PR, выполнить [проверки](#contributing), согласовать момент merge: новый каталог `environments/dev/apps/*` автоматически создаёт Application и может сразу начать deploy. При переносе работающего сервиса сначала нужна проверка совпадения ресурсов владельцем платформы.
7. Запросить GitHub-доступы из [таблицы](#status), включая runner `47_runner` и build environment `dev-build`.
8. В приложении добавить workflow по [ci-templates/application-caller.yaml](ci-templates/application-caller.yaml): указать свой `service`, заменить тестовую заглушку реальными командами. Имя файла (`ci-dev.yml` или `kubernetes-release.yml`) не определяет поведение — важны trigger и вызов reusable workflow.
9. Выпустить коммит в `dev`, проверить полный путь от build до smoke/status и возможность отката. Compose deploy — независимый workflow в приложении; этот репозиторий не отключает его автоматически.

Все aliases сервиса обновляются одним PR: нельзя отправлять половину релиза. Для нескольких сервисов в одном source-репозитории использовать отдельный вызов reusable workflow на сервис.

<a id="contributing"></a>
## Проверки и правила PR

Рендер не требует доступа к кластеру, registry или Vault. Нужны Git, Python 3.12 и `kubectl` с Kustomize. Команды для Linux / WSL из корня репозитория:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyYAML==6.0.3 yamllint==1.37.1

python scripts/validate.py --root .
python -m unittest discover -s tests -v
python scripts/generate-smoke-tests.py --check
yamllint .
git diff --check
```

Посмотреть итог одного сервиса или всего dev, ничего не применяя:

```bash
kubectl kustomize environments/dev/apps/chat-storage
kubectl kustomize environments/dev
```

`bash scripts/render.sh environments/dev/apps/chat-storage` — wrapper над `kubectl kustomize`. `bash scripts/validate.sh` дополнительно запускает `kubeconform`, если он установлен; иначе явно сообщает, что схемы не проверены. Для полного локального lint также нужны `shellcheck` и `actionlint`. Эталонный набор инструментов и версий задан в [validate.yaml](.github/workflows/validate.yaml).

CI проверяет:

- рендер всех app/platform/prerequisite overlays, полного dev, root и adoption;
- точные digest first-party images, отсутствие `latest`, `CHANGE_ME` и непредусмотренных подстановок;
- контракт сервисов, ключи Vault, дубли ресурсов и NodePort в полном рендере;
- отсутствие `Secret.data` / `stringData` и запрещённых runtime-параметров в ConfigMap;
- unit-тесты, соответствие сгенерированных smoke Jobs, YAML / shell / Actions lint;
- схемы Kubernetes и CRD через `kubeconform` с зафиксированными источниками;
- обнаружение закоммиченных секретов через Gitleaks.

Наличие image digest проверяется отдельно в promotion / rollback на runner с доступом к registry. Обычный PR CI не проверяет живой Vault, работоспособность внешних БД или итоговые ресурсы всех upstream Helm charts: их результат проверяется в Argo.

В PR описывать: какой сервис затронут, что уже нужно в Vault, будет ли перезапуск, есть ли миграция и как вернуть работоспособность. Изменения общих workflows, `argocd/`, `cluster/`, `operators/` и будущего prod требуют внимания владельцев из [CODEOWNERS](.github/CODEOWNERS).

Не коммитить kubeconfig, локальные `.env`, `service.env`, credentials, приватные ключи и rendered/comparison artifacts. Не менять имена PVC, selectors и NodePort заодно с обновлением образа. Не использовать `kubectl apply -k environments/dev` как альтернативный deploy: это обходит порядок hooks и управление Argo.

<a id="troubleshooting"></a>
## Частые ситуации

| Ситуация | Что проверить / сделать |
|---|---|
| Build зелёный, новой версии нет | Найти dispatch, promotion PR и его merge; затем revision и операцию Argo. Build не ожидает окончания deploy |
| `Rejected stale build` | Изменился HEAD `dev`. Проверить workflow нового SHA; не продвигать старое событие вручную |
| `Immutable tag already exists` | Образ SHA опубликован, в том числе возможна частично успешная multi-image сборка. Нужен новый source-коммит; не перезаписывать тег |
| PR не сливается автоматически | Проверить required checks / review, разрешение auto-merge и конфликт с `main`; не отключать проверки ради merge |
| `OutOfSync`, но `Healthy` | Работает версия, отличающаяся от Git. Посмотреть diff, auto-sync и последнюю операцию |
| `Synced` / `Healthy`, но релиз подозрителен | Проверить SHA, `Succeeded` полной операции, PostSync smoke и изменённую функцию приложения |
| Миграция неуспешна | Проверить prerequisites, готовность Secret/БД и логи migration Job. Не удалять PVC и не обходить hook |
| Smoke неуспешен | Логи оставшегося Job, Pods и зависимости; исправление либо осознанный rollback. Автоматического возврата образа нет |
| Smoke Job `NotFound` после успеха | Успешные hooks удаляются; смотреть результат операции Argo |
| `.env.development` отсутствует | Проверить ConfigMap, mountPath/subPath и `APP_ENV`, а не только переменную окружения |
| Статус есть только в deploy | Проверить `Forward deployment status`, точный source SHA, доступ status App и два `ARGO_STATUS_*` Secrets в deploy |
| Argo CLI: `token is expired` / connection refused | Проблема клиентского доступа к API Argo, не доказательство остановки GitOps. Использовать Argo UI или обновить вход с владельцем доступа |

Дополнительно: [Argo auto-sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/), [порядок hooks](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/), [GitHub commit statuses](https://docs.github.com/en/rest/commits/statuses).
