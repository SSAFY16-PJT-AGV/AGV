# 실기체 현장 시험 매뉴얼 (2m × 1m 맵)

이 순서대로 하면 기본 보정부터 막다른 길 되돌아 나오기, 급한 가지(V자) 들어가기, 자율 탐색까지 하나씩 확인할 수 있다. 각 단계는 **명령 → 통과 기준 → 안 되면 고칠 값** 순서다. 앞 단계를 통과해야 다음 단계 결과를 믿을 수 있다.

보정 명령의 자세한 설명은 [README.md](README.md)의 "현장 순서"에 있다. 이 문서는 시험 순서와 판정 기준을 다룬다.

## 0. 준비

**가져갈 것**: 줄자, 마스킹테이프(시작 위치 표시), 노트북(영상 재생·맵 뷰어), 충전된 배터리

**Jetson에서 코드 받기** (저장소 루트):

```
git remote set-url origin https://github.com/SSAFY16-PJT-AGV/AGV.git   # 저장소 주소가 바뀜(한 번만)
git fetch origin
git checkout feature/topo-mapping
git pull
python3 -m unittest discover -s agv/tests      # sim/mat/이 없으면 시뮬레이션 테스트는 건너뜀
```

**안전**
- `ask`·`drive` 모드는 `--enable-motors`를 붙여야만 바퀴가 돈다.
- **Ctrl+C**를 누르면 모터를 세우고 기록을 닫는다. 첫 주행은 손을 로봇 옆에 두고 한다.
- 주피터 노트북 커널이 카메라를 잡고 있으면 먼저 끈다.

**실시간 화면**: 아래 모든 `agv.explore` 명령에 `--view`를 붙이면, 터미널에 나온 `http://<젯슨 IP>:8080`을 노트북·폰 브라우저로 열어 검출 화면·주행 상태·이벤트를 실시간으로 볼 수 있다. 화면의 **정지** 버튼은 Ctrl+C와 같다. 접속이 안 되면 `ssh -L 8080:localhost:8080 <사용자>@<젯슨 IP>`로 접속한 뒤 `http://localhost:8080`을 연다. ask 모드의 갈림길 질문은 여전히 SSH 터미널에서 답한다(웹 화면에도 "질문 대기"가 뜬다).

**방향 약속**: 이 문서에서 위·아래·왼쪽·오른쪽은 **도안을 본 방향**이다. 로봇은 도안에서 시계 방향으로 돈다.

큰 바퀴 순서(시계 방향):

- **위 선**: 주황 → S_a(왼쪽 고리 오른쪽 변이 아래로 갈라짐) → 합류(M_b, S자 선이 올라와 붙음) → 보라 → 렌즈(짧게 갈라졌다 합쳐짐) → 초록
- **오른쪽 변**: 아래로 내려와 **오른쪽 아래 모서리**(S자 선이 급하게 갈라짐 = V자)
- **아래 선**: 노랑(S자 선과 교차) → 교차 한 번 더 → 합류(M_c, 왼쪽 고리 오른쪽 변이 붙음) → 빨강 → 빨강 스퍼 → 파랑 → 왼쪽 변을 올라가 주황

정확한 기대 맵은 뷰어(`mapping/viewer/index.html`)의 예시 맵(시뮬레이션 결과)을 본다.

로봇 진행 방향으로 보면 스퍼는 모두 바깥쪽(**왼쪽**)이고, S자 가지는 **오른쪽**이다.

## 1. 기본 보정 (README 1~5단계)

| 순서 | 명령 | 통과 기준 |
| --- | --- | --- |
| 카메라 자세 | `python3 -m agv.tools.camtool snap` | `data/snap/debug.png`에서 가로줄마다 초록 점이 하나씩 찍힘 |
| 색 6개 (자동) | `python3 -m agv.explore --mode passive --record --view`로 손으로 밀어 한 바퀴 → `python3 -m agv.tools.autocolor data/runs/<시각>` → 확인 후 `--write` | 여섯 색이 다 잡히고, `autocolor.png` 이름표가 실제 패치와 맞고, "새 설정" 패치 순서가 실제로 지나간 순서와 같음 |
| 속도 | `python3 -m agv.tools.camtool speed --enable-motors` | 나온 값을 `robot.speed_mm_s`에 적음 |
| 판정 지점 거리 | `snap`의 파란 가로줄이 바닥 어디인지 줄자로 잼 | 그 값을 `geom.ctrl_ahead_mm`에 적음(궤도 가운데 기준) |

> `robot.speed_mm_s`와 `geom.ctrl_ahead_mm`은 막다른 길에서 후진하는 거리와 V자에서 회전을 시작하는 지점을 정한다. 대충 넣으면 4·5단계가 어긋난다.

## 2. 검출만 확인 — 손으로 밀기 (모터 없음)

```
python3 -m agv.explore --mode passive --record --view
```

주황 앞에 두고, 도안의 시계 방향으로 맵을 한 바퀴 손으로 민다. 한 바퀴를 7~10초쯤(초당 15cm 안팎)으로 실제 주행 속도와 비슷하게 민다.

결과는 이렇게 본다:

```
python3 -m agv.tools.runlog data/runs/<시각>
```

**통과 기준**
- [ ] `본 색`에 여섯 색이 모두 나온다.
- [ ] 노랑 근처에 `cross`가 나온다. 노랑 패치 위에서 S자 선과 교차해 패치 속성으로 흡수될 수도 있다.
- [ ] 오른쪽 아래 모서리에서 `vee_seen side=right`가 **한 번** 나온다. `cont`는 4 이상이어야 하며, 시뮬레이션에서는 9~13이었다.
- [ ] 빨강 앞 합류(왼쪽 고리 오른쪽 변이 아래 선에 붙는 곳)에서는 `vee_seen`이 **안 나온다**.

**안 되면**
- **모서리에서 `vee_seen`이 없음**: 너무 천천히 밀었을 수 있다(판정이 프레임 수 기준이라서). 3단계 `ask`에서 다시 확인한다. 거기서도 안 나오면 7절 "영상으로 값 맞추기"를 참고한다.
- **색이 틀림**: 1단계 HSV를 다시 잰다.

## 3. 라인 추종 — 갈림길만 사람이 고르기

```
python3 -m agv.explore --mode ask --enable-motors --record --view
```

갈림길에서 멈추고 `[l]왼쪽 / [r]오른쪽`을 묻는다. Enter를 누르면 추천 쪽으로 간다. 이 단계에서는 원래 길(큰 고리)만 고르면서 한 바퀴를 돈다.

**통과 기준**
- [ ] 곡선과 교차에서 선을 놓치지 않고 한 바퀴를 돈다. `runlog`에 `gap reason=line_lost`가 없어야 한다.
- [ ] 노랑 위 교차를 지날 때 꺾이지 않고 직진한다.

**안 되면**: 흔들리면 `follow.base_speed`를 0.3에서 0.25로 낮추고 `follow.kp`를 조정한다. 속도를 바꿨으면 1단계 속도 측정을 다시 한다.

## 4. 막다른 길 되돌아 나오기 (빨강 스퍼)

**시작 위치**: 왼쪽 고리 오른쪽 변의 중간쯤에 놓고, 아래(빨강 쪽)를 보게 한다.

```
python3 -m agv.explore --mode ask --enable-motors --record --view
```

1. 빨강 근처에서 갈림길을 물으면 **`l`(스퍼 쪽)**을 고른다.
2. 로봇은 스퍼 끝에서 멈추고, 후진하고, 제자리에서 돌아 원래 선으로 돌아와 파랑으로 간다.

스퍼를 갈림길이 아니라 `tee`로 보고 그냥 지나가면 묻지 않는다. 이것도 정상이다. 이 단계는 갈림길로 물어볼 때만 시험할 수 있다.

**통과 기준** (`runlog`의 "막다른 길" 칸, 3~5번 반복)
- [ ] `dead_end side=left spur_mm=…`가 나오고, 줄자로 잰 스퍼 길이와 ±5cm 안이다.
- [ ] `되돌아 나오기 완료 1 / 시작 1`이다.
- [ ] 그 뒤 `patch color=blue`가 나온다(원래 선으로 복귀).

**안 되면**

| 증상 | 고칠 값 (`config/explore.json`의 `recover`) |
| --- | --- |
| 스퍼 끝을 못 알아보고 매트 밖으로 나감 | `end_frames` 3 → 2. 스퍼가 600mm보다 길면 `window_mm`를 늘림 |
| 후진이 비뚤어짐 | `k_slope` 0.04 → 0.03(흔들림) 또는 0.05(덜 따라감). `reverse_speed`를 낮춤 |
| 갈림길보다 덜 물러나서 회전 뒤 스퍼로 다시 들어감 | `back_extra_mm` 10 → 30. 후진이 전진보다 느린 로봇일수록 늘린다 |
| 회전이 너무 지나치거나 덜 돎 | `pivot_speed` 0.22 → 0.18, `pivot_center` 0.15 → 0.10 |

## 5. 급한 가지(V자) 들어가기 (S자 선 시작점)

**시작 위치**: 오른쪽 고리 오른쪽 변에서, 오른쪽 아래 모서리보다 40cm쯤 위에 놓고 아래를 보게 한다. 테이프로 표시해 두고 매번 같은 자리에서 시작한다.

```
python3 -m agv.explore --mode ask --enable-motors --record --view
```

모서리에 다가가면 `갈림길이에요(급한 가지(V자)가 오른쪽에 있음)`이라고 묻는다.

**5-1. `r`(가지로 들어가기)** — 3~5번 반복
- [ ] 묻는 위치에서 10cm 남짓 더 가서 선이 만나는 점 위에 서고, 오른쪽으로 제자리 회전한다.
- [ ] 회전 뒤 **S자 선**을 탄다. 노랑 패치를 비스듬히 지나 교차하는 쪽이다. 원래 모서리 곡선(아래 선)을 다시 타면 실패다.
- [ ] `runlog` "V자" 칸에서 `들어가기 완료 1 / 회전 시작 1`이다.

**5-2. `l`(원래 길 계속)** — 2~3번 반복
- [ ] 멈칫하지 않고 모서리를 돌아 아래 선을 따라 노랑으로 간다.

**안 되면**

| 증상 | 원인·고칠 값 |
| --- | --- |
| 아예 묻지 않음 | `vee_seen`이 없으면 7절 방법으로 `junction.vee_max_frames`(5)·`vee_cont_px`(4)를 맞춘다 |
| 만나는 점보다 일찍 또는 늦게 회전 | `geom.ctrl_ahead_mm`, `robot.speed_mm_s` 측정을 다시 한다 |
| 회전 뒤 원래 곡선을 탐 | `recover.pivot_speed`를 낮추고 `pivot_center`를 0.10으로 줄인다 |
| 회전 뒤 S자 선을 지나쳐 계속 돎 | `pivot_center`를 0.20으로 늘린다 |
| 그래도 불안정 | 이 가지만 시작 각도를 20~30도로 완만하게 다시 붙이면 일반 갈림길로 처리된다(시뮬레이션 수정안 `ssafy_v2_fix`) |

## 6. 자율 탐색 + 맵 만들기

**시작 위치**: 주황 패치 조금 앞, 시계 방향을 보게 놓는다.

```
python3 -m agv.explore --mode drive --enable-motors --record --max-time 400 --view
python3 -m agv.tools.runlog data/runs/<시각>
python3 mapping/build_map.py data/runs/<시각>/events.jsonl -o maps/field_v2.json --map-id field_v2
```

`maps/field_v2.json`을 노트북의 `mapping/viewer/index.html`에 끌어다 놓는다.

**통과 기준**
- [ ] `종료 이유: explore_explored`가 나온다. `max_laps`나 `line_lost`면 실패다.
- [ ] `build_map` 결과가 시뮬레이션 기대값과 같다: 패치 6, 갈라짐 3, 합쳐짐 3, 교차 2(같은 자리 한 쌍). 스퍼(T)는 0~1개다.
- [ ] `explore.complete`가 true다(미탐색 가지 없음).
- [ ] 큰 구간 길이(파랑→주황, 초록→모서리)를 줄자로 잰 값과 비교해 ±5% 안이다.

`보라에서 나가는 길 2개` 경고는 보라 스퍼가 렌즈 분기에 가까워서 생긴다. 시뮬레이션에서도 가끔 나므로 이 경고 하나만 있으면 통과로 본다.

## 7. 영상으로 값 맞추기 (노트북)

`--record`로 찍은 `camera.mp4`를 노트북에서 다시 돌리면 로봇 없이 문턱값을 바꿔 볼 수 있다. 녹화 fps가 주행과 같아서 프레임 수 기준 판정도 그대로 재현된다.

```
cp agv/config/explore.json /tmp/try.json            # junction.vee_cont_px 등을 고쳐 본다
python3 -m agv.explore --camera video:data/runs/<시각>/camera.mp4 --config /tmp/try.json --out data/replay
# --view를 붙이면 브라우저(http://localhost:8080)에서 실제 속도로 보면서 재생
python3 -m agv.tools.runlog data/replay --all
```

- **모서리에서 `vee_seen`이 안 나옴**: `--all`로 그 시각 근처에 `merge`가 찍혔는지 본다. 찍혔다면 옆 선이 천천히 붙은 것이다. 이때는 `vee_max_frames`를 7로 올려 본다.
- **엉뚱한 곳에서 `vee_seen`**: 그 `cont` 값보다 `vee_cont_px`를 높인다. 단, 모서리의 `cont`보다는 낮게 유지한다.
- 고친 값은 `agv/config/explore.json`의 `junction` 아래에 적는다(`DEFAULT_CONFIG`를 덮어씀).

## 8. 결과 공유

시험한 주행마다 `data/runs/<시각>/` 폴더를 통째로 압축해 공유한다. 폴더에는 `events.jsonl`, `config_used.json`, `camera.mp4`, `debug/`가 있다. 어느 단계 시험인지와 통과 여부를 한 줄 적는다. 영상이 있으면 노트북에서 그대로 재현해 원인을 찾을 수 있다.
