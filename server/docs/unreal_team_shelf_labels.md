# 가상 품목 표시

`add_team_shelf_labels.py`는 [6개 선반 가상 씬](unreal_team_shelf_simulation.md)을
준비한 **독립 Unreal 사본**에만 우유·휴지·달걀·주스의 영문 이니셜을 상품에
부착하고, 목적 선반에 품목과 선반 ID를 적는다. 어두운 네 색과 글자를 함께
사용한다. 실제 물품 분류나 센서 인식 표시가 아니다.

설치 전 스크립트는 v3 맵, 각 가상 소스 슬롯과 목적 선반의 품목, 대상
프로젝트, Content가 독립 디렉터리인지 확인한다. 한 씬에 한 번만 적용하며,
게임 월드의 재고·집기·적재 프로토콜은 변경하지 않는다. 레벨 파일을
수정하므로 원본이나 팀 공용 Content에 실행하지 않는다.

서버 저장소에서 `AGV_TEAM_FULL_PROJECT`를 별도 프로젝트 폴더의 절대 경로로,
`AGV_TEAM_FULL_CONFIRM`을 `ISOLATED`로 설정한다. UE 5.7의
`UnrealEditor-Cmd.exe`로 독립 `.uproject`를 열면서
`-ExecutePythonScript=<서버 저장소>/server/tools/add_team_shelf_labels.py`
를 전달한다. 이후 같은 방식으로 `verify_team_shelf_labels.py`를 실행해
저장된 라벨의 개수·내용과 상품 부착 관계를 확인한다.

정면에 가까운 v3 기본 카메라에서 품목명과 선반 ID가 보인다. 화면이 작으면
마우스 휠로 확대한다. 출발 선반의 네 상품은 가까이 놓여 이니셜이 작고,
카메라를 크게 돌리면 글자가 보이지 않을 수 있다. 라벨의 시각적 정확성은
코드 자동 검증만으로 보장되지 않는다.
