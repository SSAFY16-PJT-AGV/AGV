# 맵 JSON 형식 통일 제안

- 상태: **제안** (서버·트윈 담당 검토 전)
- 작성: 2026-11-12
- 관련: [설계 문서 v0.3](../design.md) 4장, PR #40(탐색기 맵 생성), `feature/server-communication`(관제 서버), `feature/digital-twin`(언리얼 트윈, #39)

## 1. 요약

지금 팀 안에 맵 JSON 형식이 네 갈래다. 단위(mm / m)부터 좌표 필드 이름, 엣지 형상 표현, 방향성, 구역까지 서로 다르다. 그래서 탐색기가 만든 맵을 서버와 트윈이 같이 읽지 못한다.

제안은 다음과 같다.

- **기준 형식은 설계 문서 4장과 서버가 이미 쓰는 형식으로 한다.** 단위 mm, `pos`, `shape`, `bidirectional`, `lanes`, `zones`다. 서버가 이 형식을 안전 규칙까지 엄격하게 검사하고 있어서 고칠 코드가 가장 적다.
- **트윈이 읽는 필드는 같은 파일에 함께 생성한다.** `x`, `y`, `points`(m)와 엣지 `type`이다. 트윈은 이 네 가지만 읽고 나머지는 무시한다.
- **맞추는 일은 맵을 만드는 쪽(탐색기 내보내기)이 한다.** 서버와 트윈 코드는 바꾸지 않는다.

이 문서의 예시 맵([5장](#5-예시))을 서버의 실제 코드(`MapGraph`, `plan_segment`)와 트윈 로더 검사에 넣어 둘 다 통과하는 것을 확인했다.

## 2. 현황: 누가 무엇을 읽나

| 쪽 | 위치 | 읽는 필드와 검사 |
| --- | --- | --- |
| 설계 문서 v0.3 | `docs/design.md` 4장 | 팀 기준 형식. 이 형식을 그대로 읽는 코드는 아직 없음 |
| 관제 서버 | `server/routing.py`, `server/segments.py`, `server/coordinator.py` | 아래 규칙 중 하나라도 어기면 **맵 전체를 거부** |
| 디지털 트윈 | `Source/agvSimulation/WarehouseMapLoader.cpp` | `Content/Data/warehouse_map.json` 고정 경로. 필드가 없으면 **맵 전체를 안 읽음** |
| 탐색기 | `agv/topo/export.py`, `mapping/build_map.py` (PR #40) | 맵을 만드는 쪽. 지금은 트윈 형식에 맞춰 내보냄(agv-map-v1 초안) |

**서버 검사 규칙**

- `map_id`: 비어 있지 않은 문자열이고 `@`가 없어야 한다. `version`: 0 이상 정수.
- `unit`이 정확히 `"mm"`여야 한다.
- 노드: `id`가 겹치지 않는 문자열, `pos`가 숫자 2개 배열이어야 한다.
- 엣지
  - `from`·`to`가 서로 다른, 있는 노드여야 한다.
  - `bidirectional`은 true/false여야 한다.
  - `shape`는 점 2개 이상인 폴리라인이고, 첫 점과 끝 점이 노드 `pos`와 **정확히 같아야** 한다(허용 오차 0).
  - 길이는 `shape`에서 계산한다.
- **같은 두 노드를 같은 방향으로 잇는 엣지가 둘이면 거부**한다("multiple edges in one direction are ambiguous").
- 구간 계획(`plan_segment`)
  - 모든 엣지에 양의 정수 `lanes`가 필요하다.
  - `zones` 배열이 필수다(`waiting`은 `node`, `shared`는 `nodes`).
  - 대기 노드가 공유 구역에 들어 있으면 거부한다.
  - 경로 끝이 대기 노드여야 한다.
- JSON 크기 4MB 이하, 중첩 깊이 32 이하, 키 중복 금지.

**트윈 로더 규칙**

- `nodes[]`: `id`(문자열), `x`, `y`(숫자, m). 읽을 때 ×100 해서 언리얼 cm로 바꾼다.
- `edges[]`: `from`, `to`(문자열), `type`(문자열), `points`(배열. 원소는 `{x, y}`, m. 양 끝 노드를 뺀 중간 점).
- 위 필드 말고는 읽지 않는다(`map_id`, 노드 `type`·`color`·`group` 등은 예시 파일에만 있음).
- 경로 찾기는 엣지를 양방향으로 해석한다(코드 주석: 단방향은 추후 확장).
- 로봇 위치 메시지(`x`, `y`, `yaw`)에는 `ExternalPositionScale`(기본 100, m 기준)을 곱한다. 에디터에서 바꿀 수 있는 값이다.

## 3. 항목별 차이와 조정 방향

| 항목 | 설계 v0.3 | 서버 | 트윈 | 탐색기(현재) | 조정 |
| --- | --- | --- | --- | --- | --- |
| 길이 단위 | mm | mm (필수 검사) | m | m | **기준 mm**. 트윈용 m 좌표는 생성 |
| 노드 좌표 | `pos: [x, y]` | `pos` 필수 | `x`, `y` 필수 | `x`, `y` | 둘 다 씀: `pos`(mm) + `x`, `y`(m) |
| 엣지 형상 | `shape` (양 끝 포함) | `shape` 필수, 양 끝 = 노드 `pos` | `points` 필수 (중간 점, m) | `points: []` | 둘 다 씀: `shape`(mm) + `points`(m) |
| 엣지 길이 | 형상에서 | `shape` 길이 | 안 씀 | `length_m`(주행 거리) | `length_mm`(실측)를 더함. [7장](#7-합의가-필요한-것) 3번 |
| 방향성 | `bidirectional` | `bidirectional` 필수 | 없음(양방향) | `directed` | **`bidirectional`로 통일**, `directed`는 뺌 |
| 차선 | `lanes` | `lanes` 필수 | 없음 | `lanes` | 그대로 |
| 엣지 종류 | 없음 | 안 읽음 | `type` 필수 | `type` | 그대로(`main`, `destination`) |
| 같은 방향 평행 엣지 | 정한 적 없음 | 거부 | 허용 | 렌즈에 있음 | **중간 노드를 넣어 피함**([4.4](#44-탐색기가-자동으로-채우는-것)) |
| 구역 `zones` | `shared`, `waiting` | 필수 | 없음 | 없음 | 탐색기가 초안을 만들고 사람이 확인 |
| 노드 종류 | 정한 적 없음 | 안 읽음 | `type`(예시에만) | `type` + `kind` | 트윈 어휘(`access`, `junction`, `destination`)를 공통 `type`으로, 세부는 `kind` |
| 원점 | `origin_marker` | 안 읽음 | 없음 | `origin_node` | **`origin_node`**(v0.3에서 원점이 패치로 바뀜) |
| 좌표 방향 | 정한 적 없음 | 무관(거리만 씀) | 언리얼 방향 | +y가 오른쪽 | **+y는 +x의 오른쪽**(언리얼과 같음). [7장](#7-합의가-필요한-것) 1번 |
| `map_id`, 버전 | `map_id@버전` | `@` 금지, 정수 버전 | 안 읽음 | 있음 | 그대로 |
| 선반·벽·마커 | 있음 | 안 읽음 | 없음 | 없음 | 설계 문서 정의 유지(아직 쓰는 곳 없음) |

## 4. 제안 형식 (agv-map-v1 개정)

### 4.1 맵 전체

| 필드 | 필수 | 읽는 쪽 | 뜻 |
| --- | --- | --- | --- |
| `map_id` | O | 서버 | `@` 없는 문자열 |
| `version` | O | 서버 | 0 이상 정수. 맵이 바뀌면 올린다. 메시지에서는 `map_id@v버전` |
| `schema` | O | - | `"agv-map-v1"` |
| `unit` | O | 서버 | `"mm"`. `pos`, `shape`, `*_mm` 필드의 단위 |
| `coordinate_unit` | O | (트윈 예시) | `"meter"`. 트윈용 `x`, `y`, `points`의 단위 |
| `coordinate_frame` | O | - | `"x_forward_y_right"`([4.2](#42-좌표계)) |
| `origin_node` | O | - | 원점 노드 ID(원점 패치) |
| `pos_source` | O | - | `"odometry"`(주행 기록으로 계산, 수 cm 오차) / `"estimated"`(엣지 길이로 추정한 배치) / `"measured"`(실측) |
| `nodes`, `edges` | O | 서버·트윈 | 아래 |
| `zones` | O | 서버 | 아래. 서버 구간 계획에 필요 |
| `shelves`, `walls`, `markers` | - | - | 설계 문서 4장 정의 그대로(나중에) |
| `explore`, `warnings`, `debug` | - | - | 탐색기 진단용 |

### 4.2 좌표계

- 원점 패치가 (0, 0)이고, 원점 패치에서 나가는 진행 방향이 +x다(설계 문서 v0.3 4장). 주행 기록으로 계산한 맵(`"odometry"`)은 원점 패치의 가운데를 (0, 0), 로봇이 그 패치를 지나는 방향을 +x로 둔다.
- **+y는 +x의 오른쪽**이다. 언리얼(왼손 좌표계: X 앞, Y 오른쪽, Z 위)과 같아서, 트윈이 값을 그대로 X, Y로 써도 위에서 볼 때 실제 매트와 같은 모양이 된다(거울상 아님).
- 각도는 위에서 볼 때 시계 방향이 +다(언리얼 yaw와 같음).
- `pos`(mm)와 `x`, `y`(m)는 같은 점이다. `x = pos[0] / 1000`, `y = pos[1] / 1000`.

### 4.3 노드와 엣지

**노드**

| 필드 | 필수 | 읽는 쪽 | 뜻 |
| --- | --- | --- | --- |
| `id` | O | 서버·트윈 | 패치 `P_<색>`, 갈라짐 `S<n>`, 합쳐짐 `M<n>`, 교차 `X<n>`, 스퍼 갈림 `T<n>`, 스퍼 끝 `D_<노드>`, 중간 노드 `V_<엣지>` |
| `pos` | O | 서버 | [x, y] mm |
| `x`, `y` | O | 트윈 | m |
| `type` | O | (트윈 예시) | `access`(색 패치), `junction`(갈라짐·합쳐짐·교차·스퍼 갈림·중간 노드), `destination`(스퍼 끝) |
| `kind` | O | - | `patch`, `split`, `merge`, `cross`, `tee`, `spur_end`, `via` |
| `color` | 패치 | - | 패치 색 |
| `spur`, `branches`, `through`, `same_spot`, `crossing`, `visits` | - | - | 탐색기 정보(현재 `mapping/README.md`의 뜻 그대로) |

**엣지**

| 필드 | 필수 | 읽는 쪽 | 뜻 |
| --- | --- | --- | --- |
| `id` | O | - | 엣지 ID. `branches`, `through`가 가리킴 |
| `from`, `to` | O | 서버·트윈 | 노드 ID. 탐색기가 만든 맵에서는 탐색할 때 지나간 방향이고, `branch`·`shape` 순서도 이 방향 기준 |
| `shape` | O | 서버 | [[x, y], ...] mm. 첫 점 = `from`의 `pos`, 끝 점 = `to`의 `pos` |
| `points` | O | 트윈 | `shape`의 중간 점들([{x, y}], m) |
| `bidirectional` | O | 서버 | 기본 true. 테이프 선은 양쪽으로 따라갈 수 있고 차체가 제자리에서 돌 수 있어서, 탐색은 한 방향으로 했어도 양방향으로 둔다. 스퍼 엣지도 true(들어갔다 후진으로 나옴). 한쪽으로만 다니게 할 맵만 라인을 false로(탐색기 `--one-way`) |
| `lanes` | O | 서버 | 1 |
| `type` | O | 트윈 | `main`(라인), `destination`(스퍼) |
| `length_mm` | - | - | 실측 주행 거리 평균. `shape` 길이와 다를 수 있음 |
| `branch`, `length_std_mm`, `traversals` | - | - | 탐색기 정보 |

**구역**

| 필드 | 뜻 |
| --- | --- |
| `{"id", "type": "waiting", "node"}` | 대기 노드. 경로 끝이 될 수 있는 곳 |
| `{"id", "type": "shared", "nodes": [...]}` | 공유 구역. 한 번에 한 로봇만 |
| `auto` | 탐색기가 자동으로 만든 초안이면 true. 사람이 확인하면 지운다 |

### 4.4 탐색기가 자동으로 채우는 것

- **좌표와 형상**
  - 탐색 중 남긴 회전 기록(`trace.jsonl`)이 있으면 로봇이 지나간 궤적으로 노드 좌표와 곡선을 계산한다(`pos_source: "odometry"`). `shape`는 그 곡선(양 끝은 노드 `pos`), `points`는 곡선 중간 점이다. PR #40에 구현됨(지금은 `points`만 채움).
  - 회전 기록이 없으면 엣지 길이로 배치를 추정한다(`"estimated"`). 이때 `shape`는 양 끝 두 점짜리 직선이고, `points`는 빈 배열이다.
  - 바닥 mm 좌표계 단계가 끝나면 카메라로 잰 좌표와 곡선으로 바꾸고 `measured`로 표시한다.
- **스퍼:** 패치·스퍼 갈림마다 스퍼 끝 노드(`D_<노드>`, `destination`)와 스퍼 엣지(`bidirectional: true`)를 만든다. 스퍼 길이를 모르면 그림용 길이(150mm)로 놓고 `length_estimated: true`로 표시한다.
- **같은 방향 평행 엣지**(렌즈처럼 갈라졌다 바로 같은 곳에서 합쳐지는 구간): 두 번째 가지 가운데에 중간 노드 `V_<엣지>`(`kind: "via"`)를 넣어 엣지 두 개로 나눈다. 서버 규칙에 맞고 트윈에도 문제가 없다.
- **구역 초안** (`auto: true`)
  - 대기(`waiting`): 스퍼 끝 노드. 스퍼는 원래 작업하는 로봇이 다른 로봇에게 길을 비켜 주려고 만든 곳이라 대기 지점에 딱 맞는다.
  - 공유(`shared`): 갈라짐·합쳐짐·교차·스퍼 갈림 노드. 같은 자리 교차(`same_spot`) 두 노드는 한 구역으로 묶는다.
  - 서버 규칙대로 대기 노드는 공유 구역에 넣지 않는다.
- **내보낼 때 검사:** 서버 규칙([2장](#2-현황-누가-무엇을-읽나))과 트윈 로더 규칙을 둘 다 검사하고, 어긋나면 알려 준다.

## 5. 예시

갈라졌다 합쳐지는 짧은 구간(렌즈)과 왼쪽 스퍼가 있는 작은 맵이다. 서버 `MapGraph`(최단 경로 `P_orange → S1 → M1 → P_green`, 1300mm, 반대 방향도 같은 거리), 서버 `plan_segment`(`P_orange → D_P_orange` 구간, 반대 방향 `P_green → M1 → V_E3 → S1 → P_orange → D_P_orange` 구간), 트윈 로더 검사를 모두 통과한다.

`E3`를 `S1 → M1`로 바로 이으면 서버가 "multiple edges in one direction are ambiguous"로 거부한다. 그래서 중간 노드 `V_E3`로 나눴다.

```json
{
  "map_id": "mat_2x1", "version": 3, "schema": "agv-map-v1",
  "unit": "mm", "coordinate_unit": "meter", "coordinate_frame": "x_forward_y_right",
  "origin_node": "P_orange", "pos_source": "estimated",
  "nodes": [
    {"id": "P_orange", "pos": [0, 0], "x": 0.0, "y": 0.0,
     "type": "access", "kind": "patch", "color": "orange",
     "spur": {"side": "left", "length_mm": 180, "node": "D_P_orange"}},
    {"id": "D_P_orange", "pos": [0, -180], "x": 0.0, "y": -0.18,
     "type": "destination", "kind": "spur_end", "spur_of": "P_orange"},
    {"id": "S1", "pos": [400, 0], "x": 0.4, "y": 0.0,
     "type": "junction", "kind": "split", "branches": {"left": "E2", "right": "E3"}},
    {"id": "V_E3", "pos": [650, 120], "x": 0.65, "y": 0.12,
     "type": "junction", "kind": "via", "via_of": "E3"},
    {"id": "M1", "pos": [900, 0], "x": 0.9, "y": 0.0,
     "type": "junction", "kind": "merge"},
    {"id": "P_green", "pos": [1300, 0], "x": 1.3, "y": 0.0,
     "type": "access", "kind": "patch", "color": "green"}
  ],
  "edges": [
    {"id": "E1", "from": "P_orange", "to": "S1", "shape": [[0, 0], [400, 0]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "main", "length_mm": 402, "traversals": 4},
    {"id": "E2", "from": "S1", "to": "M1", "shape": [[400, 0], [900, 0]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "main", "branch": "left", "length_mm": 505, "traversals": 2},
    {"id": "E3", "from": "S1", "to": "V_E3", "shape": [[400, 0], [650, 120]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "main", "branch": "right", "length_mm": 290, "traversals": 2},
    {"id": "E3b", "from": "V_E3", "to": "M1", "shape": [[650, 120], [900, 0]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "main", "length_mm": 290, "traversals": 2},
    {"id": "E4", "from": "M1", "to": "P_green", "shape": [[900, 0], [1300, 0]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "main", "length_mm": 398, "traversals": 4},
    {"id": "E_D_P_orange", "from": "P_orange", "to": "D_P_orange", "shape": [[0, 0], [0, -180]], "points": [],
     "bidirectional": true, "lanes": 1, "type": "destination", "length_mm": 180}
  ],
  "zones": [
    {"id": "Z_S1", "type": "shared", "nodes": ["S1"], "auto": true},
    {"id": "Z_M1", "type": "shared", "nodes": ["M1"], "auto": true},
    {"id": "W_D_P_orange", "type": "waiting", "node": "D_P_orange", "auto": true}
  ]
}
```

## 6. 각 쪽에 미치는 영향

| 쪽 | 바뀌는 것 |
| --- | --- |
| 탐색기(PR #40) | 내보내기에 `unit`, `pos`, `shape`, `bidirectional`, `zones`, 중간 노드를 더하고 `directed`를 뺀다. 서버 규칙 검사를 더한다 |
| 서버 | 코드 변경 없음. `map_file`에 탐색기 맵을 지정하면 된다 |
| 트윈 | 코드 변경 없음. 맵 파일을 `Content/Data/warehouse_map.json`에 넣으면 된다. 레벨에서 예시 맵 노드 ID(`J01` 등)를 직접 지정한 곳이 있으면 새 ID로 바꾼다 |
| 설계 문서 | 합의 후 4장 맵 JSON 예시와 표를 이 형식으로 바꾼다 |

## 7. 합의가 필요한 것

1. **좌표 방향.** +y를 +x의 오른쪽(언리얼과 같음)으로 정할지. 트윈을 안 바꾸려면 이쪽이어야 한다. 로봇 위치 메시지(`state`의 x, y, θ)에도 똑같이 적용한다. 추천: 이렇게 정한다.
2. **트윈용 중복 필드.** 장기적으로 트윈이 `pos`, `shape`(mm)를 직접 읽게 하고 `x`, `y`, `points`를 없앨지. 트윈 로더의 ×100을 ×0.1로 바꾸는 정도의 수정이다. 로봇 위치 배율(`ExternalPositionScale`)은 에디터에서 0.1로 바꿀 수 있다. 추천: 당장은 중복으로 두고, 트윈이 안정되면 정리한다.
3. **경로 거리.** 서버는 `shape` 길이로 거리를 잰다. 실측 좌표가 나오기 전에는 좌표가 추정이라, 직선 `shape` 길이가 실제 주행 거리와 10~30% 다를 수 있다. 선택지는 둘이다.
   - (가) 그동안 경로를 고르는 데만 쓰고 감수한다.
   - (나) 서버가 `length_mm`가 있으면 그것을 쓴다. 서버 코드 몇 줄을 고쳐야 한다.

   추천: (가). 실측 좌표 단계 뒤에는 둘이 같아진다.
4. **구역 확정 절차.** 자동으로 만든 대기·공유 구역(`auto: true`)을 누가 어떤 기준으로 확인할지. 서버 문서도 실제 안전 구역 크기·정지 거리는 사람이 확인해야 한다고 적고 있다.
5. **노드 ID 안정성.** 패치 ID(`P_<색>`)는 맵을 다시 만들어도 같지만, 갈림길 번호(`S1`, `M2` 등)는 탐색 순서에 따라 바뀔 수 있다. 서버 설정이나 트윈 레벨이 갈림길 ID를 직접 가리키면 맵을 바꿀 때마다 고쳐야 한다. 추천: 작업 지점은 패치·스퍼 끝 ID로만 가리킨다.
6. **양방향 주행.** 엣지를 기본 양방향으로 둔다(PR #40에 반영). 트윈은 원래 양방향으로 보고, 서버는 반대 방향 경로도 계획하며 엣지 예약을 방향과 상관없이 잡으므로 마주 오는 두 로봇이 한 엣지에 같이 들어가지 않는다. 남는 것: (가) 로봇이 탐색과 반대 방향으로 갈 때 갈라짐이 합쳐짐으로 보이고 좌우가 바뀌는 것을 주행 쪽에서 처리하는 방법, (나) 급한 가지(V자)를 반대로 지날 때의 제자리 회전, (다) 한쪽으로만 다니게 할 구간을 정할지. 추천: 양방향으로 두고, 막히는 구간만 나중에 `false`로 바꾼다.

## 8. 진행 순서

1. 이 제안을 서버·트윈 담당과 검토한다(7장).
2. 탐색기 내보내기를 이 형식으로 고친다(PR #40). 서버 규칙 검사를 더하고, 시뮬레이션 맵으로 서버 `MapGraph`·`plan_segment`와 트윈 로더 검사를 통과하는지 테스트한다.
3. 합의 내용을 설계 문서 4장에 반영한다.
4. 바닥 mm 좌표계 단계에서 카메라로 잰 좌표와 곡선 형상으로 바꾼다(`pos_source: "measured"`). 그 전까지는 주행 기록으로 계산한 값(`"odometry"`)을 쓴다.
