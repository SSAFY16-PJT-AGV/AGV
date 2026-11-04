# agv/ — JETANK 탐색 주행

로봇이 바닥 라인을 따라 돌면서 색 패치와 갈림길(갈라짐·합쳐짐)을 검출해 이벤트 로그로 남기고, 안 가 본 가지를 골라 매트 전체를 탐색한다. 맵은 그 로그로 `mapping/build_map.py`가 만든다.

Jetson Nano 기본 이미지(Python 3.6, OpenCV, jetbot)에서 돌아가며, 추가 설치는 필요 없다.

## 구성

| 파일 | 내용 |
| --- | --- |
| `explore.py` | 실행 진입점. 카메라 → 인식 → 이벤트 기록 → 모터 |
| `runner.py` | 한 프레임 처리 로직. 시뮬레이터(`sim/`)도 같은 코드를 쓴다 |
| `linecam.py` | 라인·색 패치·갈림길 검출, 라인 추종 조향 |
| `topo/` | 이벤트 로그 형식, 탐색 정책(안 가 본 가지 우선), 맵 빌더 |
| `hw.py` | 카메라(GStreamer / jetbot / 녹화 영상), 모터(jetbot Robot), 팔 서보 |
| `tools/camtool.py` | 현장 보정: 화면 저장, HSV 측정, 속도 측정 |
| `config/explore.json` | 현장 값(속도, HSV, 카메라). `linecam.DEFAULT_CONFIG`를 덮어쓴다 |

## 현장 순서

저장소 루트에서 실행한다. 주피터 노트북이 카메라를 잡고 있으면 먼저 그 커널을 끈다.

1. **카메라 자세 확인**: `python3 -m agv.tools.camtool snap`
   - 팔을 숙여 카메라가 차체 앞 바닥을 보게 한다. `data/snap/debug.png`에서 라인이 화면 아래 절반의 가운데쯤 보이고, 가로줄마다 초록 점이 하나씩 찍혀야 한다.
   - 이 자세를 주행 자세로 정하고, 서보 각도를 `config/explore.json`의 `arm.servo_angles`에 적어 두면 `--set-arm`으로 다시 맞출 수 있다.
   - GStreamer가 안 열리면 `--camera jetbot`으로 바꾼다(이때는 노출 고정이 안 됨). JetPack이 오래돼 `aelock`을 모르면 `camera.lock_exposure`를 `false`로 둔다.
2. **색 재기**: 패치 4개와 라인 위에서 각각 `snap`을 찍고 `python3 -m agv.tools.camtool hsv data/snap/raw.png x y w h`로 HSV를 잰다. 결과로 `patch.colors`(H 범위)와 `line.v_max`를 고친다. 주황(H 0~18)과 노랑(19~38)이 가까우니 특히 확인한다.
3. **검출만 확인(모터 없음)**: `python3 -m agv.explore --mode passive --record`
   - 로봇을 손으로 밀어 한 바퀴 돌린다. 터미널에 `patch`(4색), `split`, `merge` 이벤트가 찍혀야 한다.
   - 결과 폴더의 `debug/*.jpg`에서 검출 상태를, `camera.mp4`로는 노트북에서 다시 돌려 볼 수 있다: `python3 -m agv.explore --camera video:<폴더>/camera.mp4`
4. **속도 재기**: `python3 -m agv.tools.camtool speed --enable-motors` → 간 거리를 입력해 나온 값을 `robot.speed_mm_s`에 넣는다. 방향이 반대면 `robot.invert_left/right`를 고친다.
5. **갈림길만 사람이 고르는 주행**: `python3 -m agv.explore --mode ask --enable-motors`
   - 갈라지는 곳에서 멈추고 왼쪽/오른쪽을 묻는다. 라인 추종이 흔들리면 `follow.base_speed`를 낮추고 `follow.kp`를 조정한다.
6. **자율 탐색**: `python3 -m agv.explore --mode drive --enable-motors --record`
   - 갈림길마다 안 가 본 쪽을 고르고, 모든 가지를 다 돈 뒤 같은 패치로 한 바퀴 깨끗하게 돌아오면 그 패치에서 멈춘다(`end` 이벤트의 `reason`이 `explore_explored`).
7. **맵 만들기**: `python3 mapping/build_map.py data/runs/<시각>/events.jsonl -o maps/<이름>.json` → `mapping/viewer/index.html`에 끌어다 놓는다.

Ctrl+C를 누르면 모터를 세우고 `end` 이벤트를 남긴 뒤 끝난다. ask·drive 모드는 `--enable-motors` 없이는 실행되지 않는다.

## 검출 방식과 조정 값

카메라는 앞 바닥을 비스듬히 본다(화면 아래 = 가까움). 스캔 영역에 가로줄 14개를 긋고 줄마다 라인 조각을 찾는다.

- **라인**: 어둡고(`v_max`) 채도 낮은(`s_max`) 픽셀. 아주 어두운 픽셀(`v_dark`)은 채도와 상관없이 라인으로 본다. 어두운 곳은 노이즈로 채도가 튀어서, 이 처리가 없으면 선이 두 조각으로 쪼개진다.
- **색 패치**: 화면 아래쪽(`patch.region_top`부터)에서 그 색 비율이 `on_frac`을 넘으면 진입 이벤트를 낸다.
- **갈림길**: 조향 줄(`line.control_row`, 차체 바로 앞)에서 조각 수가 바뀌는 순간을 본다.
  - 1개에서 2개로 바뀔 때 두 조각이 붙어 있고 간격이 벌어지면 **갈라짐**이다. 이때 탐색 정책이 갈 쪽을 고른다.
  - 2개에서 1개로 바뀔 때 두 조각이 붙어 있었으면 **합쳐짐**이다.
  - 옆에서 다른 선이 들어오거나 나가는 경우(간격이 좁아지거나 처음부터 멂)는 무시한다.
  - 갈라짐 이벤트의 `onset`(간격/라인폭 비율, 간격 변화 px)을 보고 `junction.split_*` 값을 조정한다. 시뮬레이션에서 갈라짐은 비율 1.2~1.7·변화 +15 이상, 옆 선 진입은 비율 1.8 이상·변화 음수였다.
- 앞쪽에 갈래 모양이 보이는 것(`fork_ahead`)만으로는 방향을 정하지 않는다. 합류 지점에 옆 가지로 들어갈 때도 화면상 똑같이 보여서, 그걸 믿으면 반대 가지로 역주행한다(시뮬레이터에서 실제로 겪음).

## 이벤트 로그

`data/runs/<시각>/events.jsonl`에 한 줄에 하나씩 기록된다. 형식은 `topo/events.py` 맨 위에 있다.

```json
{"ts": 1730700000.85, "seq": 1, "type": "patch", "odo_mm": 127.0, "color": "orange"}
{"ts": 1730700012.95, "seq": 3, "type": "split", "odo_mm": 1735.2, "choice": "left", "cmd": "left", "onset": {"ratio": 1.23, "trend": 16.0}}
{"ts": 1730700014.10, "seq": 5, "type": "merge", "odo_mm": 2013.4, "from_side": "left"}
```

`odo_mm`은 모터 명령 × `speed_mm_s`를 적분한 추정 이동거리다. passive 모드에서는 `null`이고, 이때 빌더가 시간 × 속도로 대신 계산한다.

## 테스트

```
python3 -m unittest discover -s agv/tests -v
```

실제 모터·카메라는 쓰지 않는다. `test_sim_e2e.py`는 가상 매트(`sim/`)에서 탐색부터 맵 생성까지 돌린다.
