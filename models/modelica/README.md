# 플랜트 대표 수력 회로 Modelica 교차 검증 (D2-B → D4)

> 2026-11-25 PU-D5a: 원료 펌프가 두 대(CDU·RFCC 병렬, HOU·RHDS·VRDS 직렬)로 늘었다. 이 문서의 단일 펌프 회로 수치·검증은 그 전 기록이다. 현재 회로는 `config/physics_*_integrated.yaml` 의 `station` 과 README 의 '두 번째 원료 펌프' 절을 본다.

2026-11-24부터 모델 파일은 범용 `PlantHydraulics.mo`(결합 `Circuit`, 단일 `SingleCircuit`, 펌프 NPSH·캐비테이션 포함)이며 다섯 플랜트 설정(`config/physics_<plant>_integrated.yaml`)을 각각 비교한다. `--all` 은 `<output>/<plant>` 에, `--cdu-d0` 는 CDU 회로 묶음(결합·P-8303A·F-8201)을 회로별 하위 폴더에 쓴다. 이번 실행 결과는 [REPORT.md](REPORT.md) 마지막 절에 있다.

D2-A의 `config/physics_cdu_integrated.yaml`을 **입력값 원본**으로 사용한다. `PlantHydraulics.mo`(이전 이름 `CduHydraulics.mo`)는 연결점의 압력 연속과 유량 보존을 Modelica 커넥터로 표현한다. OpenModelica가 이 연립방정식을 풀고, Python은 기존 이분법으로 같은 운전점을 계산한다. Python 해는 Modelica 초기값이나 입력값에 전달하지 않는다. Modelica의 순방향 유량 시작 추정값은 각 설정의 입력 펌프·후속 펌프 설계 정격 유량과 그 차이(입력값)다. 두 실행의 물리적 초기 조건은 동일한 정상 상태 경계압과 고정 회전수이며 열·질량 저장 상태는 없다.

[Modelica.Fluid 표준 라이브러리](https://doc.modelica.org/om/Modelica.Fluid.html)의 `PrescribedPump`, `StaticPipe`, `ValveIncompressible`를 검토했다. 기본 매질·마찰·밸브 특성 및 저유량 정칙화가 이번 고정 계수 비교에 섞이지 않도록 이 조건만 표현하는 최소 요소를 작성했다. 역방향 방정식의 부호는 수치 탐색을 돕기 위해 정의했고, 결과는 순방향 해로 제한한다.

## 설치와 재실행

공식 macOS 페이지는 macOS 13 이상 universal 실험 빌드를 안내한다. GUI가 필요 없다면 공식 배포 디렉터리의 `OpenModelica-CLI-latest-macos-universal.tar.gz`를 풀어 CLI를 사용할 수 있다. Xcode Command Line Tools의 C 컴파일러를 확인한다. XQuartz는 이번 헤드리스 실행에 필요하지 않다. [OpenModelica macOS 안내](https://openmodelica.org/download/download-mac/) · [공식 CLI 배포 디렉터리](https://build.openmodelica.org/omc/rust/latest/)

이 작업 공간의 네이티브 설치 경로는 `.cache/openmodelica/native/bin/omc`다. 새 작업 공간에서는 저장소 루트에서 아래와 같이 설치한다. 다운로드 경로는 최신 실험 빌드를 가리킨다. 이번에 사용한 정확한 버전과 파일 해시는 [검증 보고서](REPORT.md)에 기록했다.

```sh
xcrun --find clang
mkdir -p .cache/openmodelica
curl --fail --location \
  https://build.openmodelica.org/omc/rust/latest/OpenModelica-CLI-latest-macos-universal.tar.gz \
  --output .cache/openmodelica/macos-universal.tar.gz
mkdir .cache/openmodelica/native
tar -xzf .cache/openmodelica/macos-universal.tar.gz -C .cache/openmodelica/native
.cache/openmodelica/native/bin/omc --version
```

저장소 루트에서 실행한다. 결과 경로는 존재하지 않는 새 경로로 지정한다. Python 환경은 프로젝트의 기존 잠금 의존성을 사용한다.

```sh
uv run --locked python -m models.modelica.verify \
  --omc "$PWD/.cache/openmodelica/native/bin/omc" --all --output .cache/openmodelica/verification
```

macOS CLI를 사용할 수 없을 때 [공식 Docker 안내](https://openmodelica.org/download/docker/)의 이미지에서 ARM64 매니페스트를 확인한다. 아래 래퍼는 확인된 Linux ARM64 이미지 다이제스트를 고정하고, 현재 결과 디렉터리만 마운트한다.

```sh
docker buildx imagetools inspect openmodelica/openmodelica:v1.27.1-minimal
docker pull --platform linux/arm64 openmodelica/openmodelica@sha256:a7f9f5f747a6a2ee98bbace787a6946e03d61282fee8ac1058ee71adce05b89d
uv run --locked python -m models.modelica.verify \
  --omc "$PWD/models/modelica/omc-docker-arm64.sh" \
  --output .cache/openmodelica/verification-docker
```

자동화는 `omc --version`, `Smoke.mo`를 컴파일·적분한 값 `x(1) ≈ exp(-1)`, 정상·밸브 제한·정상 처리량 감소·임펠러 마모·회전수 두 지점·막힘의 일곱 정상 운전점을 확인한다. 마모·처리량 감소를 추가하기 전의 결과는 REPORT.md에 기록돼 있다. 시간에 따른 고장 단계, 센서 잡음·지연·누락, 캐비테이션/NPSH는 이 비교 범위 밖이다. 결과 폴더에 생성된 Modelica 입력, 실행 스크립트, 컴파일러 로그, 원시 결과 CSV와 비교 CSV/JSON이 남는다.

## 합격 기준

| 비교 항목 | 절대 허용오차 바닥값 |
| --- | --- |
| 주 유량과 분기 유량 | 10⁻⁸ m³/s |
| 흡입·헤더·후속 토출 절대압 | 1 Pa |
| 주 펌프 양정 | 10⁻⁴ m |
| 수력·축동력 | 0.1 W |

항목별 판정 허용오차는 위 절대값과 `|Python 값| × 10⁻⁶` 중 큰 값이다. 이는 절대압과 동력 스케일 차이를 감안해 두 코드의 수치 일치를 확인하는 기준이다. 수지 잔차에는 상대오차를 쓰지 않는다. 두 모델 각각의 잔차 절댓값이 질량 10⁻⁶ kg/s, 펌프·분기 압력 1 Pa, 기계적 에너지 0.1 W 이하여야 한다. 허용오차는 장비 측정 정확도 기준으로 해석하지 않는다.
