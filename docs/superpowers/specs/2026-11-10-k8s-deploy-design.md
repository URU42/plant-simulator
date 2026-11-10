# k8s 배포 설계 — 파드 하나로 다섯 공장, 공유 네임스페이스 안에서

작성일 2026-11-10. 앞선 설계(`2026-11-09-multi-plant-design.md`)의 결과물을 네임스페이스 `u4-2`(CPU 2 · Memory 4Gi, frontend · backend · AI agent 와 공유)에 올린다.
다른 프로젝트가 HTTP API 로 값을 읽고, 브라우저에서 디버깅 화면을 볼 수 있어야 한다.

## 1. 의도

- 자원 예산의 대부분을 다른 서비스에 남긴다. 이 서비스는 **파드 하나**(수집 서버 + 다섯 공장 생성기 in-process)로 CPU 500m · Memory 1Gi 한도 안에서 돈다.
- HDS 는 배포 대상에서 뺀다(RHDS·VRDS 로 대체됨). 구성안 파일과 테스트는 남긴다.
- 외부 노출은 Ingress(`u4-2.skala-us.com`, 접두어 `/plant-sim`)로 조회 API·화면·SSE 만. 쓰기(`/ingest`)는 토큰 없이는 받지 않는다.
- 코드의 시뮬레이터 ↔ 전송부 ↔ 수집 분리는 유지한다. compose·스크립트 분리 실행도 그대로 된다.

## 2. 목표 / 비목표

목표
- 통합 실행: 수집 서버가 `--with-simulators` 로 공장 목록의 생성기를 같은 프로세스에서 돌린다. 전송부는 HTTP 대신 같은 프로세스의 수집 함수를 부른다.
- 배포 설정: `ROOT_PATH`(경로 접두어), `CORS_ORIGINS`, `INGEST_TOKEN` 환경 변수.
- k8s 매니페스트(`deploy/k8s/`): Deployment(replicas 1, Recreate, resources, probes), Service(ClusterIP), PVC(gp3 20Gi), Ingress(SSE 설정, 접두어 제거), Secret 예시, kustomization.
- 이미지 빌드·Harbor push 워크플로(GitHub Actions)와 PC 에서 빌드하는 명령.
- 메모리 상한에 맞게 신호 모델 블록 캐시를 1일로.

비목표
- 인증 전반(조회 API 는 공개), HTTPS 인증서(Ingress 쪽 설정), 오토스케일, 다중 replica.
- DB 를 다른 파드가 직접 읽는 것(RWO). 읽기는 HTTP API 로.
- frontend · backend · agent 의 매니페스트.

안티 목표
- 네임스페이스 한도를 이 서비스가 절반 넘게 쓴다.
- 접두어 아래에서 화면 링크·리다이렉트가 깨진다.
- `/ingest` 가 인터넷에 열려 아무나 값을 넣는다.

## 3. 제약 (네임스페이스 `u4-2`)

- CPU 2 · Memory 4Gi, requests·limits 합계 기준. 기본값(100m/128Mi, 500m/512Mi)에 기대지 않고 명시한다.
- LoadBalancer 금지, Ingress 만. host `u4-2.skala-us.com`.
- Pod Security baseline: privileged·hostPath·hostNetwork 없음. 이미지는 PC 또는 GitHub Actions 에서 빌드해 Harbor 로.
- 이미지 pull: 기본 ServiceAccount 에 `harbor-cred` 연결됨. ServiceAccount 를 따로 쓰지 않는다.
- PVC: gp3(`ebs-sc`), RWO, 20Gi 까지. 하나만 쓴다.
- 기존 의존성만. 코드 변경은 작게.

## 4. 결정 사항

### 4.1 통합 실행 (`--with-simulators`)

| 곳 | 변경 |
| --- | --- |
| `simulator/sender.py` | `LocalSender(receive: Callable[[IngestBatch], Awaitable[dict]], collector_time: Callable[[], str | None])` — `send()` 가 HTTP 대신 수집 함수를 바로 부른다. `collector_time()` 은 그 공장의 `sim_now`. `close()` 는 아무 것도 안 한다 |
| `collector/plant_context.py` | 수신 핵심을 `PlantContext.receive(batch_id, source_id, samples: list[Sample], received_at) -> IngestResult` 로 둔다: 보관 기간 밖 행 거르기 → 저장 → 배치 전진(상한 넘으면 따라잡기 태스크) → 구독자가 있을 때만 SSE 발행. HTTP 라우트는 `RawBatch` 를 행 단위 검증한 뒤 이 핵심을 부르고, `LocalSender` 는 생성기의 `Sample` 을 그대로 넘긴다(JSON 직렬화·재검증 없음) |
| `collector/app.py` | `create_app(plants, settings)`. `settings.with_simulators` 면 lifespan 에서 공장마다 `asyncio.create_task(run_mode(..., LocalSender(ctx)))` 를 띄우고 종료 때 취소한다. 태스크가 예외로 끝나면 로그를 남기고 프로세스를 끝낸다(k8s 가 재시작, 재개 로직이 이어 채운다) |
| 첫 백필과 이벤트 루프 | 백필 시계는 기다리지 않으므로 `LocalSender` 경로는 창 하나를 통째로 동기 처리한다. 그래서 통합 실행의 백필 창은 1시간이 아니라 **10분**(`Simulator(window_sec=600)`, 1초 공장 약 4만 샘플 ≈ 창당 0.5초 이하)으로 두고, `Simulator.run` 이 창을 보낸 뒤 `await asyncio.sleep(0)` 로 양보한다. 그래야 백필 중에도 `/health`·조회가 수백 ms 안에 응답하고 메모리 피크(창 하나의 샘플·행)가 작다. 다섯 공장 하루치 = 창 720개, CPU 500m 에서 약 10~20분 |
| 시간 모드·백필 기간 | 통합 실행 인자 `--time-mode`(기본 각 runtime 값), `--backfill-days`(기본 각 runtime 값). k8s 는 `backfill`, 1일. 나중에 늘리려면 PVC 의 DB 를 지우고 다시 채워야 한다(재개는 마지막 시각부터만). README 에 적는다 |
| 시뮬레이터 전용(평가용) 시나리오 | 환경 변수 `SIM_SCENARIO_DIR`(예: `/app/private`). 있으면 그 안의 `scenario_<key>.yaml` 을 그 공장의 시뮬레이터에만 더 준다(수집 서버 이력 적재에는 주지 않는다 — 기획서 8장). k8s 에서는 Secret 을 그 경로에 선택적으로 마운트한다. 파일이 없으면 그냥 지나간다 |
| CLI | `python -m collector.app --plants config/plants.k8s.yaml --with-simulators [--time-mode backfill] [--backfill-days 1]` (`WITH_SIMULATORS=1` 환경 변수도 됨) |
| 생성 부하 | 공장당 1초에 약 6 ms. 다섯 공장 합쳐 3 % CPU |

HTTP 전송 경로(`HttpSender`)는 그대로 남아 compose·테스트·외부 시뮬레이터에 쓴다.

### 4.2 배포 설정 (환경 변수, `collector/settings.py` 새 파일)

| 변수 | 뜻 | 기본 |
| --- | --- | --- |
| `ROOT_PATH` | Ingress 접두어(예: `/plant-sim`). FastAPI `root_path` 로 넘기고, 루트 목록 페이지 링크·`/{key}` 리다이렉트·`/docs` 가 접두어를 붙여 만들어진다 | 빈 문자열 |
| `CORS_ORIGINS` | 쉼표 구분 origin 목록. 있으면 `CORSMiddleware`(GET·HEAD, 자격 증명 없음) | 없음(미들웨어 없음) |
| `INGEST_TOKEN` | 있으면 `POST /{key}/ingest` 는 헤더 `X-Ingest-Token` 이 같아야 한다(403). `LocalSender` 는 토큰을 거치지 않는다 | 없음(토큰 검사 없음) |
| `WITH_SIMULATORS` | `1` 이면 `--with-simulators` 와 같다 | 없음 |

`HttpSender` 는 `INGEST_TOKEN` 환경 변수가 있으면 같은 헤더를 붙인다(외부 시뮬레이터용).

### 4.2a 조회 API 보강

다른 프로젝트가 DB 를 직접 읽지 못하므로(RWO) 기획서가 Agent 가 읽는다고 둔 이력 표를 API 로 연다:
`GET /{key}/maintenance`(`maintenance_history` 전체, `equip_id` 필터), `GET /{key}/failures`(`failure_history` 전체, `equip_id` 필터). README API 표에 추가.

### 4.3 HDS 제외와 공장 목록

- `config/plants.yaml` 에서 hds 항목을 빼고 compose 의 `sim-hds` 도 뺀다. `plant_hds.yaml`·`runtime.yaml`·`scenario_dev.yaml` 은 테스트 자산으로 남긴다(README 에 적는다).
- k8s 용 `config/plants.k8s.yaml`: 같은 다섯 공장, `db: /app/data/<key>.db`. runtime 은 `time_mode: backfill` 인 복사본이 아니라 기존 파일을 쓰고 `--time-mode` 를 통합 실행 인자로 준다(`--time-mode backfill`).
- 화면·API 는 변경 없음.

### 4.4 메모리

`simulator/rfcc_model.BLOCK_CACHE_DAYS` 와 `random_quality.BLOCK_CACHE_DAYS` 를 2 → 1. 날짜 경계에서 전날 블록은 예열용 혁신값만 다시 만들므로(캐시 불필요) 값은 바뀌지 않는다. 예상 RSS: 공장당 45 MB(1초 태그 65개 기준) × 5 + HOU 품질 블록 + SQLite 캐시 ≈ 400 MB. 한도 1Gi.

### 4.5 k8s 매니페스트 (`deploy/k8s/`, kustomize)

| 파일 | 내용 |
| --- | --- |
| `deployment.yaml` | `plant-sim`, replicas 1, strategy Recreate. 컨테이너 `image: harbor.uru42.space/uru42/plant-simulator:<TAG>`(kustomize 로 바꿈). args `--plants config/plants.k8s.yaml --with-simulators --time-mode backfill --backfill-days 1 --port 8000`. env `ROOT_PATH=/plant-sim`, `INGEST_TOKEN`(Secret `plant-sim` 의 키 `ingest-token`, optional), `CORS_ORIGINS`(선택), `SIM_SCENARIO_DIR=/app/private`. resources requests 200m/512Mi, limits 500m/1Gi. PVC 를 `/app/data` 에, Secret `plant-sim-scenarios`(optional) 를 `/app/private` 에 마운트. startupProbe `GET /health` 30초 간격 × 60회(첫 백필 30분까지 허용, timeout 10초), readinessProbe `GET /health` 10초(timeout 5초), livenessProbe `GET /health` 30초(timeout 10초, 실패 5회). 백필 창이 10분이고 창마다 양보하므로 프로브가 백필 중에도 응답을 받는다 |
| `service.yaml` | ClusterIP `plant-sim`, 8000 |
| `pvc.yaml` | `plant-sim-data`, `ebs-sc`, RWO, 10Gi (원시값 3.5 GB 고정 + 집계 2 GB/년; 처음 20Gi 는 과했다) |
| `ingress.yaml` | nginx, host `u4-2.skala-us.com`, path `/plant-sim(/|$)(.*)` → `/$2`(rewrite-target), 주석: `proxy-buffering: "off"`, `proxy-read-timeout: "3600"`, `proxy-send-timeout: "3600"` |
| `secret.example.yaml` | `plant-sim`(ingest-token)·`plant-sim-scenarios`(scenario_<key>.yaml) Secret 모양(값은 비움). 실제 값은 커밋하지 않는다 |
| `kustomization.yaml` | 위 리소스, `images:` 로 이미지 이름·태그 치환 |

다른 프로젝트의 호출 경로: backend·agent 는 클러스터 안에서 `http://plant-sim.u4-2.svc.cluster.local:8000/cdu/current`(접두어 없음, CORS 불필요), 브라우저 frontend 는 `https://u4-2.skala-us.com/plant-sim/cdu/current`(CORS 필요).

### 4.6 이미지 빌드

- `Dockerfile` 은 그대로(이미 `config/` 를 복사한다). `deploy/k8s/` 는 복사하지 않는다.
- 기본 경로는 PC 빌드: `docker build --platform linux/amd64 -t harbor.uru42.space/uru42/plant-simulator:<TAG> . && docker push …` 를 README 에 적는다.
- CI/CD 는 팀의 Jenkins(`Jenkinsfile`: rootless-docker 에이전트, Harbor 로봇 자격, being-gitops `k8s/plant-sim` 태그 갱신)로 한다. GitHub Actions 워크플로는 쓰지 않는다(사용자 결정, 2026-11-10).

### 4.6a compose

기본 서비스는 통합 실행 컨테이너 하나(`plant-sim`, `--with-simulators`, 네임드 볼륨). 분리 실행(수집 서버 + 시뮬레이터 다섯)은 `profiles: [split]` 로 남겨 `docker compose --profile split up` 으로 띄운다. **내 판단**: §1 의 "분리 실행도 그대로" 와 §5.7 의 모순을 이렇게 푼다.

### 4.7 문서

README 에 "k8s 배포" 절: 통합 실행 명령, 환경 변수 표, 매니페스트 적용 순서(`kubectl apply -k deploy/k8s -n u4-2`), 자원 예산, 첫 백필 소요, 외부/내부 호출 주소, HDS 제외 안내.

## 5. 검증

1. `LocalSender` 로 통합 실행한 앱(`create_single_app(..., with_simulators=True)` 테스트 모드: `live_until` 짧게)이 몇 초 안에 그 공장의 `tag_current` 를 채운다. HTTP 경로 테스트는 그대로 통과.
2. `INGEST_TOKEN` 이 있으면 헤더 없는 `/ingest` 는 403, 맞는 헤더는 200, `LocalSender` 는 영향 없음.
3. `ROOT_PATH=/plant-sim` 이면 루트 목록 링크가 `/plant-sim/cdu/`, `/cdu` 리다이렉트가 `/plant-sim/cdu/`. 화면의 상대 경로는 그대로.
4. `CORS_ORIGINS` 가 있으면 `Access-Control-Allow-Origin` 응답 헤더.
5. 블록 캐시 1일에서 모델 값·자정 연속성 테스트 그대로 통과.
6. `kustomize build deploy/k8s`(또는 `kubectl kustomize`)가 성공하고 resources 합계가 한도 안(테스트에서 yaml 을 읽어 계산).
7. 로컬 실제 실행: `--with-simulators` 로 다섯 공장을 한 프로세스로 띄워 백필 중에도 `/health` 가 1초 안에 응답하는지, 끝난 뒤 다섯 공장 live 인지, RSS 를 README 에 적는다. compose 통합 실행 컨테이너로도 확인한다.
7a. `/maintenance`·`/failures` 응답, `SIM_SCENARIO_DIR` 의 파일이 시뮬레이터에만 적용되고 이력에는 안 들어가는지, `--backfill-days` 반영.
8. 클러스터 적용은 사용자가 한다(kubeconfig·Harbor 자격은 이 세션에 없다). 적용 뒤 확인 명령을 README 에 적는다.

## 6. 작업자에게 맡기는 것

- `receive_batch` 의 위치(`collector/app.py` 안 함수 vs `plant_context.py` 메서드)와 `LocalSender` 가 `IngestBatch` → `RawBatch` 로 바꾸는 방법(`model_dump` 한 번).
- 통합 실행에서 공장 태스크 하나가 예외로 죽을 때의 처리: 로그를 남기고 프로세스를 끝낸다(k8s 가 재시작). 백필 재개 로직이 이어서 채운다.
- 매니페스트의 레이블·주석 모양, kustomize 의 이미지 치환 방식.

## 7. 사용자가 채울 값

- Harbor 레지스트리 주소와 프로젝트 경로(`harbor.uru42.space/uru42`). 매니페스트는 자리표시자로 둔다.
- `INGEST_TOKEN` 값(Secret 으로 직접 만든다).
- 접두어 `/plant-sim` 을 다른 것으로 바꿀지. **내 판단으로 `/plant-sim`.**
- frontend 의 origin(CORS). 모르면 비워 두고 클러스터 내부 호출만 한다.
