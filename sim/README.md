# sim/ — 가상 매트 시뮬레이터

실물 없이 탐색 주행부터 맵 생성까지 시험하는 도구다. 위에서 본 매트 그림에서 로봇 카메라 시점 영상(원근 포함)을 잘라 만들고, 실물과 똑같은 `agv/runner.py`에 넣어 모터 명령을 받는다. 그 명령으로 궤도형 차체를 움직이며, 미끄러짐과 속도 오차도 넣는다.

```
pip install opencv-python numpy        # 노트북
python3 sim/run_sim.py --out data/sim_run --video
python3 mapping/build_map.py data/sim_run/events.jsonl -o data/sim_run/map.json
```

| 옵션 | 결과 |
| --- | --- |
| (기본) | `events.jsonl`(실물과 같은 형식), `gt.json`(정답 노드 위치·경로 길이·궤적), `mat_topdown.png` |
| `--video` | `run.mp4`: 왼쪽은 검출 결과를 그린 카메라 시점, 오른쪽은 위에서 본 궤적 |
| `--raw-video` | `camera.mp4`: 원본 카메라 영상. `python3 -m agv.explore --camera video:...`로 재생 시험할 때 쓴다 |
| `--seed N` | 노이즈·미끄러짐 난수 |

`mats/ssafy_mat.json`은 `docs/images/track-mat.jpg` 사진을 눈대중으로 본뜬 매트다. 큰 루프 하나에 분기 두 곳(노랑 패치 위 갈라짐, 보라 패치 위 합쳐짐)이 있다. 실측 치수가 나오면 이 파일을 고친다. 경로는 점을 지나는 부드러운 곡선(Catmull-Rom)으로 그린다.

카메라 시야는 `run_sim.py`의 `CAM`이다(앞 90~380mm, 폭 180~460mm). 실물 카메라 자세를 정한 뒤 실제 시야로 맞추면 시뮬레이션이 더 실물에 가까워진다.
