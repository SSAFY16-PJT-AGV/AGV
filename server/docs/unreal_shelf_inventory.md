# Unreal 선반 점유 스냅샷 제안

이 패치는 웹 주문 시연에서 Unreal의 선반 점유를 조회하기 위한 로컬 통신
제안이다. 테스트 사본의 기존 로컬 인증·동작 ACK·위치 검증 패치를 먼저
적용한 상태에 적용한다. 실물 재고나 실물 로봇의 안전을 보장하지 않는다.

인증된 Unreal 연결에 서버가 다음 요청을 보낸다.

```json
{"type":"shelf.snapshot.request","data":{"request_id":"요청별 고유 ID","shelf_ids":["SHELF_RED","SHELF_BLUE"]}}
```

Unreal은 요청된 ID에 해당하는 선반만 찾아 현재 게임 월드의 슬롯 점유를
게임 스레드에서 읽고 동일한 연결에 다음 형식으로 응답한다. 슬롯 배열에서
`true`는 점유, `false`는 비어 있음을 뜻한다. 배열 길이는 선반의 실제 슬롯
개수다. 아래 값은 형식 예시이며 실제 빈 슬롯을 뜻하지 않는다.

```json
{"type":"shelf.snapshot","data":{"request_id":"요청별 고유 ID","shelves":[{"shelf_id":"SHELF_RED","product_type":"Red","slots":[true,false,false,false,false,false]},{"shelf_id":"SHELF_BLUE","product_type":"Blue","slots":[false,false,false,false,false,false]}]}}
```

슬롯 배열이나 슬롯 컴포넌트가 유효하지 않으면 응답하지 않는다. 요청 ID가
비었거나 너무 길거나 요청 선반 ID가 비었거나 중복되면 무시한다. 요청된
선반이 없으면 그 항목은 결과에서 빠지고, 같은 ID의 선반이 둘이면 둘 다
반환된다. 서버는 누락·중복·제품 종류 불일치·예상과 다른 슬롯 개수와 늦은
응답을 거부해야 한다. 이전 소켓에서 온 요청은 새 소켓에 응답하지 않는다.
이 스냅샷만으로 외부에서 발생한 미래의 상태 변경이나 물리 재고를 보증할 수
없으며, 실제 적재 직전에는 기존 Unreal의 슬롯 점유 확인을 계속 사용한다.

## 적용 순서

실행 중인 에디터나 원본 팀 작업물에 임의로 적용하지 않는다. 별도 테스트
프로젝트에서 `dcc44ae` 기준 소스와 세 기존 패치를 사용한다. 이미 세 패치가
적용된 테스트 사본이라면 마지막 `git apply --check`부터 확인한다.

```powershell
$server = 'D:\관통\AGV-analysis\server\docs'
cd '<별도 Unreal 테스트 프로젝트>'
git apply --check --unidiff-zero "$server\unreal_local_auth.patch"
git apply --unidiff-zero "$server\unreal_local_auth.patch"
git apply --check "$server\unreal_action_ack.patch"
git apply "$server\unreal_action_ack.patch"
git apply --check "$server\unreal_action_position_guard.patch"
git apply "$server\unreal_action_position_guard.patch"
git apply --check "$server\unreal_shelf_inventory.patch"
git apply "$server\unreal_shelf_inventory.patch"
```

에디터가 소스 적용 대상 프로젝트를 사용 중이면 기존 작업을 보존한 뒤 닫고
적용·재빌드해야 변경이 반영된다. 팀 원본 브랜치는 별도로 관리하며 이 패치는
자동으로 팀 브랜치에 적용되거나 업로드되지 않는다.

## 적용 가능성 확인

원본 소스에 기존 세 패치와 이 패치를 순서대로 적용할 수 있음을 확인했다.
별도의 Unreal 5.7 프로젝트 사본에서 C++ 빌드도 성공했다. 이 사본의 실제
Unreal 게임 프로세스(`-game -nullrhi`)와 격리 MQTT·웹 서버를 연결해 선반
스냅샷을 받은 뒤 빨강 상품 두 건을 연속 및 동시 주문했고, 실제 Unreal 로그에서
서로 다른 슬롯의 집기·적재 성공을 확인했다. 화면을 표시하지 않은 시험이며
원본 팀 프로젝트는 수정하지 않았다. 실제 창고의 재고나 안전 검증은 아니다.
