# Urban Assistant Deploy

Русский | [English](README.md)

Приватный GitOps-репозиторий конфигурации платформы «Помощник
проектировщика». Он описывает желаемое состояние Kubernetes-кластеров, но не
содержит исходный код приложений. Git является единственным источником истины:
GitHub Actions не получает `kubeconfig` и не подключается к control-plane, а
Argo CD читает проверенные изменения из ветки `main` и приводит кластер к этому
состоянию.

Сейчас активно только окружение `dev`. Каталог `environments/prod` — неактивный
каркас для будущего отдельного production-кластера и отдельного экземпляра
Argo CD.

## Как устроена доставка

```text
репозиторий приложения                 urban-assistant-deploy

push в защищённую dev
        |
        +--> тесты
        +--> build всех images релиза
        +--> push dev-<full-sha> ----------> on-prem registry
        +--> получение sha256 digest
        +--> repository_dispatch ----------> проверка services.yaml
                                              проверка HEAD ветки dev
                                              проверка digest в registry
                                              bot PR со всеми digest релиза
                                                        |
                                                        v
                                              validation + auto-merge
                                                        |
                                                        v
                                                     main
                                                        |
                                                        v
                                                     Argo CD
                                                        |
                                      PreSync migration -> rolling update
                                                        |
                                                        v
                                                  Kubernetes dev
```

Важное следствие: тег образа не считается версией развёртывания. Версией
является точный `sha256` digest, записанный в Git. Благодаря этому один коммит
всегда описывает один воспроизводимый набор контейнеров.

Для сервисов с несколькими связанными образами, например API и migrator,
digests обновляются одним PR. Частично обновить такой релиз workflow не
позволяет.

## Модель репозитория

| Путь | Что находится внутри | Когда изменять |
|---|---|---|
| `apps/<service>/base/` | Нейтральные Deployment, Service, Job, PVC и monitoring-ресурсы | Когда меняется общая для всех окружений модель приложения |
| `environments/dev/apps/<service>/` | Dev ConfigMap, endpoints, ресурсы и точные image digests | Когда меняется только dev-конфигурация или версия образа |
| `environments/dev/prerequisites/` | Vault secrets, Redis и PVC, которые должны существовать до приложения | При изменении предварительных зависимостей Urban API и PZZ |
| `cluster/` | Кластерные ресурсы, например StorageClass | Для изменений уровня Kubernetes-кластера |
| `platform/` | Общие Kafka, Vault, Gateway и monitoring-ресурсы | Для общей инфраструктуры платформы |
| `environments/dev/platform/` | Dev-патчи платформенных ресурсов | Для адресов и параметров платформы, специфичных для dev |
| `operators/` | `releases.yaml` и values зафиксированных Helm-релизов | Для добавления или обновления операторов |
| `argocd/bootstrap/` | Однократная установка и корневое Argo Application | При bootstrap или переключении adoption/steady state |
| `argocd/adoption/` | Ручная синхронизация без auto-sync и prune | Только во время первоначального усыновления ресурсов |
| `argocd/root/` | Рабочие AppProject, Application и ApplicationSet | Для постоянного GitOps-управления |
| `services.yaml` | Машиночитаемый контракт между приложениями и deploy-репозиторием | При добавлении сервиса, image или изменении build-контракта |
| `vault-contract.yaml` | Полный список всех Vault paths и читаемых keys без значений | При любом изменении `get .Secrets` |
| `.github/workflows/` | CI, обработка promotion event и reusable release workflow | При изменении процесса доставки |
| `ci-templates/` | Минимальный caller workflow для репозиториев приложений | При изменении способа подключения приложений |
| `scripts/` | Рендер, проверки, promotion и операционные helpers | При изменении автоматизации |
| `tests/` | Unit-тесты контракта обновления image | Вместе с изменениями `update-image.py` |

### Base и overlay

В `base` нет адресов конкретного окружения и реальных имён registry. Для
first-party images используются логические имена, например
`urban-assistant/urban-api`. Dev overlay подменяет их через Kustomize:

```yaml
images:
  - name: urban-assistant/urban-api
    newName: 10.32.11.13:5000/urban_api
    digest: sha256:<64-hex-digest>
```

Общее правило:

- изменение подходит всем окружениям — меняется `apps/<service>/base`;
- capacity, внутренний Service DNS, Vault path или image окружения — меняется overlay;
- значение внешнего runtime endpoint меняется в Vault, а его key фиксируется в `vault-contract.yaml`;
- каждый credential, TLS key и другой `get .Secrets` также обязан быть в `vault-contract.yaml`;
- в `base` нельзя возвращать dev-адреса, реальные registry paths и секреты;
- `latest` и first-party image без digest запрещены проверками.

## Контракт `services.yaml`

`services.yaml` связывает четыре сущности: репозиторий исходного кода, build
образа, Kustomize overlay и Argo CD Application. Для каждого сервиса каталог
задаёт:

- разрешённый GitHub repository и обязательную ветку `dev`;
- путь приложения в `environments/dev/apps`;
- health endpoint;
- наличие migration Job и prerequisites;
- полный атомарный список images релиза;
- для каждого image — alias, логическое Kustomize-имя, registry repository,
  build context, Dockerfile и target.

Promotion workflow принимает событие только для пары service/repository,
которая есть в каталоге. Он также проверяет, что переданный commit всё ещё
является HEAD ветки `dev`. Поэтому поздно завершившийся старый build не может
откатить окружение.

При изменении `services.yaml` нужно особенно внимательно проверить Dockerfile,
build context, image alias и список `atomicImages`: этот файл исполняется как
контракт доставки, а не является справочной документацией.

## Argo CD

Bootstrap использует Helm chart `argo-cd 10.4.1` и Argo CD `v3.5.2`.
`scripts/bootstrap-argocd.sh` останавливается, если версия Kubernetes ниже
1.25. UI остаётся `ClusterIP`; штатный доступ — через VPN и port-forward:

```bash
kubectl port-forward -n argocd svc/argocd-server 8080:443
```

`argocd/root/applicationset-apps.yaml` автоматически создаёт отдельное Argo
Application для каждого каталога `environments/dev/apps/*`. Поэтому сбой или
rollout одного сервиса не объединяется с rollout остальных. Отдельные
Applications управляют cluster foundation, operators, Vault integration,
monitoring, Kafka, Gateway и prerequisites.

Операторы генерируются из `operators/releases.yaml`. Большинство Helm charts
загружаются из upstream repository, а values читаются из этого Git-репозитория
через Argo CD multiple sources. Chart Vault Secrets Operator зафиксирован и
хранится в Git, потому что кластер не имеет доступа к Helm repository HashiCorp.

### Adoption и steady state

Изначально `argocd/bootstrap/root-application.yaml` указывает на
`argocd/adoption`. В этом режиме Applications создаются без автоматического
sync и prune. Каждый компонент сначала сравнивается с живым кластером и
усыновляется вручную.

После завершения adoption-аудита root Application переключается на
`argocd/root`. Тогда включаются:

- автоматическая синхронизация;
- `selfHeal: true` — исправление ручного drift в кластере;
- `prune: true` — удаление объектов, удалённых из Git;
- `allowEmpty: false` — защита от случайного пустого render.

До завершения adoption нельзя включать prune: существующий ресурс, которого
ещё нет в Git, иначе может быть удалён.

## Миграции

Urban API и PZZ выполняют migration Job как Argo CD `PreSync` hook. Перед
миграцией отдельно синхронизируются VaultStaticSecret, ConfigMap, Redis и PVC.

У Job заданы `BeforeHookCreation`, `HookSucceeded`, ограниченное число повторов
и deadline. Пока Job не завершился успешно, новый Deployment не начинается и
старые pods продолжают работать.

Миграции обязаны быть идемпотентными и обратно совместимыми. Git rollback
возвращает предыдущий image digest, но никогда автоматически не делает
downgrade базы данных.

## Секреты и endpoint-конфигурация

Внутренние Kubernetes Service DNS хранятся в Git: это часть желаемой топологии
кластера, а не секрет. Внешние IP и URL хранятся в Vault даже без credentials.
`vault-contract.yaml` перечисляет полный интерфейс Vault: все credentials, TLS
данные, внешние endpoints и прочие keys, которые читаются через `get .Secrets`.
Значений в контракте нет. `VaultStaticSecret` материализует данные в Kubernetes
Secret. Проверка требует точного совпадения контракта со всеми шаблонами,
запрещает Vault-only переменные в ConfigMap, dotenv внутри ConfigMap или literal
`env.value`, а также запрещает прямые внешние endpoint в шаблонах.

Запрещено коммитить:

- пароли, API tokens и credentials;
- TLS private keys и сертификаты с приватным ключом;
- `Secret.data` и `Secret.stringData`;
- `kubeconfig`, Vault token и SSH-ключ control-plane;
- GitHub App private key и registry password.

`VaultStaticSecret` содержит только путь и шаблон. Реальные значения читает
Vault Secrets Operator из внешнего Vault. GitHub credentials находятся в
GitHub Secrets. Frontend-конфигурация встраивается во время build и недоступна
Vault Secrets Operator, поэтому `MAPBOX_PUBLIC_TOKEN`,
`FRONTEND_KEYCLOAK_AUTH_URL` и `FRONTEND_KEYCLOAK_LOGOUT_REDIRECT` передаются как
защищённые GitHub Actions secrets. В `environments/dev/build/frontend.env`
остались только build-time параметры без endpoint.

GitHub Secrets доступны workflow, но недоступны обычному Kustomize render в
Argo CD. Поэтому адрес registry остаётся частью желаемого поля `image` и записан
в Git до появления внутреннего DNS-имени. В секрете хранятся только registry
username/password. Другие осознанные literal-значения — внутренние Kubernetes
Service DNS, bootstrap-адрес `VaultConnection`, Kubernetes API audience и
локальные bind/listener-адреса `0.0.0.0`. Адрес самого Vault нельзя получить из
Vault без циклической зависимости.

## Локальная работа

Рекомендуется Linux, WSL или Git Bash: операционные helpers написаны на Bash.
Для базовых проверок нужны Git, Python 3.12+, PyYAML и `kubectl` с Kustomize.
Расширенная локальная проверка дополнительно использует `kubeconform`,
`yamllint`, `shellcheck` и `actionlint`.

Перед изменениями:

```bash
git switch main
git pull --ff-only
git switch -c <type>/<short-description>
```

Собрать полный dev без `.env`:

```bash
./scripts/render.sh environments/dev > /tmp/urban-assistant-dev.yaml
```

Проверить все Kustomize targets и правила репозитория:

```bash
python3 scripts/validate.py --root .
python3 -m unittest discover -s tests -v
```

Запустить расширенную проверку, если установлен `kubeconform`:

```bash
./scripts/validate.sh
```

Перед PR полезно посмотреть именно итоговый Kubernetes diff, а не только
изменённые YAML-фрагменты:

```bash
git diff
./scripts/render.sh environments/dev > /tmp/urban-assistant-dev.yaml
```

GitHub workflow `Validate desired state` дополнительно запускает schema
validation, YAML/shell/Actions lint и Gitleaks.

## Типовые изменения

### Изменить конфигурацию dev

1. Найти overlay в `environments/dev/apps/<service>`.
2. Изменить ConfigMap, resources или patch.
3. Не переносить dev-значения в `base`.
4. Отрендерить сервис и полное окружение.
5. Создать PR в `main` и дождаться `Validate desired state`.

После merge Argo CD применит изменение автоматически только в steady-state
режиме. Во время adoption sync выполняется вручную после проверки diff.

### Выпустить новую версию приложения

В штатном процессе этот репозиторий вручную не редактируется:

1. Изменение попадает в защищённую ветку `dev` приложения.
2. Kubernetes release workflow тестирует и публикует images.
3. Workflow отправляет одно promotion event.
4. Deploy bot создаёт PR с точными digests.
5. После validation и auto-merge Argo CD запускает rollout.

Ручная правка digest допустима только как осознанная операционная процедура с
PR и теми же проверками. Нельзя выполнять `kubectl set image` как штатный
deploy: это создаёт drift вне Git.

### Добавить новый сервис

1. Создать нейтральный `apps/<service>/base/kustomization.yaml` и ресурсы.
2. Создать `environments/dev/apps/<service>/kustomization.yaml` с namespace,
   labels, dev config и image digest.
3. При необходимости создать отдельные prerequisites.
4. Добавить полный build/release-контракт в `services.yaml`.
5. Скопировать `ci-templates/application-caller.yaml` в репозиторий приложения
   как `.github/workflows/kubernetes-release.yaml` и задать service key и
   настоящий test command.
6. Выполнить полный render и validation.
7. После merge проверить новое `dev-<service>` Application в Argo CD.

ApplicationSet обнаружит новый dev overlay автоматически; отдельный Argo
Application вручную обычно не нужен.

### Обновить Helm-оператор

1. Изменить версию chart в `operators/releases.yaml`.
2. При необходимости обновить соответствующий values-файл.
3. Проверить changelog, CRD compatibility и render.
4. Во время adoption выполнить ручной diff/sync без prune.
5. В steady state провести изменение через обычный PR.

### Откатить приложение

Найти deploy-коммит, который изменил digest, и сделать Git revert через PR:

```bash
git revert <deploy-commit>
```

После merge Argo CD вернёт старый container digest. Перед откатом сервиса с
миграциями отдельно проверить совместимость текущей схемы БД со старым кодом.

## Назначение scripts

| Скрипт | Назначение |
|---|---|
| `render.sh` | Совместимый wrapper над чистым `kubectl kustomize` |
| `validate.py` | Рендер всех targets и проверки invariants/ownership/ports/digests/secrets |
| `validate.sh` | `validate.py` плюс `kubeconform`, если он установлен |
| `update-image.py` | Строгая проверка promotion payload и атомарное обновление digests |
| `export-build-contract.py` | Выдаёт build matrix одного сервиса reusable workflow |
| `verify-registry-digests.sh` | Проверяет существование переданных digests в registry |
| `render-frontend-env.sh` | Формирует контролируемый frontend `service.env` во время build |
| `bootstrap-argocd.sh` | Проверяет Kubernetes и устанавливает зафиксированный Argo CD chart |
| `adoption-diff.sh` | Выполняет read-only `kubectl diff -k` для выбранного target |
| `compare-rendered.py` | Сравнивает identity и критичные поля двух render-файлов |

## GitHub workflows

- `validate.yaml` проверяет желаемое состояние на каждом PR и push в `main`;
- `promote-dev-image.yaml` принимает `repository_dispatch`, валидирует событие,
  проверяет registry и создаёт bot PR;
- `reusable-application-release.yaml` выполняет tests/build/push/dispatch для
  репозитория приложения;
- `ci-templates/application-caller.yaml` показывает минимальный способ вызвать
  reusable workflow из приложения.

Registry jobs используют runner label `13_runner`. На runner не должно быть
`kubeconfig`, Vault token
или SSH-доступа к control-plane. Предпочтителен ephemeral runner; допустима
выделенная очищаемая VM.

## Что нужно настроить перед эксплуатацией

- защитить `main`, требовать PR и check `Validate desired state`;
- создать labels `automated` и `environment/dev`;
- создать GitHub App `deploy-bot` с минимальной записью в deploy-репозиторий;
- создать read-only GitHub App `git-reader` для проверки исходных веток и
  доступа Argo CD к Git;
- заполнить все keys из `vault-contract.yaml` в Vault;
- добавить frontend secrets `MAPBOX_PUBLIC_TOKEN`,
  `FRONTEND_KEYCLOAK_AUTH_URL` и `FRONTEND_KEYCLOAK_LOGOUT_REDIRECT`;
- подготовить self-hosted registry runner;
- добавить caller workflow в каждый репозиторий приложения;
- установить Argo CD и выполнить adoption по компонентам без prune;
- только после проверки GitOps deploy и rollback отключать старый Compose job.

Полная последовательность bootstrap и adoption описана в
[docs/GITOPS-RUNBOOK.md](docs/GITOPS-RUNBOOK.md).

## Стабильные интерфейсы

Имена ресурсов, namespaces, selectors, NodePorts и PVC существующей платформы
сохранены, чтобы Argo CD мог усыновить их без пересоздания. Диапазоны NodePort:

| Диапазон | Назначение |
|---|---|
| `31000-31049` | API приложений |
| `31050-31099` | MCP |
| `31100-31199` | UI и admin |
| `31200-31299` | Наблюдаемость |
| `31300-31399` | Gateway и edge |

Уникальность текущих фиксированных портов проверяется на полном render.

## Production

Production должен использовать отдельные Kubernetes-кластер, Argo CD, Vault
paths, capacity и approval rules. Backend digest продвигается из dev без
пересборки через ручной PR с approval. Frontend собирается отдельно, потому что
его публичная конфигурация встраивается во время build. Активного production
Application сейчас нет.
