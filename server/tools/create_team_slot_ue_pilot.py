"""Prepare an isolated Unreal milk-slot pilot from the prior v3 demo copy.

Red demo assets represent synthetic milk; this is neither real stock nor the
full four-product/six-shelf team scene. Existing project files are modified, so
an explicitly selected disposable project with a non-linked Content is required.
"""
import json
import os
from pathlib import Path
import stat

import unreal


project = Path(unreal.Paths.project_dir()).resolve()
selected = os.environ.get('AGV_TEAM_SLOT_PROJECT', '')
if os.environ.get('AGV_TEAM_SLOT_CONFIRM') != 'ISOLATED' or not selected or project != Path(selected).resolve():
    raise RuntimeError('explicit matching disposable team-slot project required')
content = project / 'Content'
if os.lstat(str(content)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    raise RuntimeError('the Content directory must not be a junction')
world = json.loads((content / 'Data/warehouse_map.json').read_text(encoding='utf-8'))
if world.get('map_id') != 'mat_ssafy_v3' or world.get('coordinate_unit') != 'meter':
    raise RuntimeError('matching v3 projection required')
nodes = {n['id']: n for n in world['nodes']}
if 'D02' not in nodes:
    raise RuntimeError('missing v3 D02')
unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/AGV/NewMap')
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
red = [a for a in actors if a.get_class().get_name().startswith('BP_RedObject_')]
red.sort(key=lambda a: a.get_actor_location().x)
shelves = [a for a in actors if a.get_class().get_name().startswith('BP_RedShelf_')]
if len(red) != 3 or len(shelves) != 1:
    raise RuntimeError('v3 demo must contain exactly three red items and one red shelf')


def replace_tags(actor, new_tag):
    tags = [tag for tag in actor.get_editor_property('tags')
            if not str(tag).startswith('TEAM_V3_SLOT_') and str(tag) != 'TEAM_V3_SHELF']
    # Changed actor tags force World Partition to persist its external package.
    marker_a, marker_b = unreal.Name('TEAM_V3_REV_A'), unreal.Name('TEAM_V3_REV_B')
    marker = marker_b if marker_a in tags else marker_a
    tags = [tag for tag in tags if tag not in (marker_a, marker_b)]
    actor.set_editor_property('tags', tags + [unreal.Name(new_tag), marker])


for index, actor in enumerate(red, start=1):
    replace_tags(actor, 'TEAM_V3_SLOT_{}'.format(index))
shelf = shelves[0]
target = nodes['D02']
shelf.set_actor_location(unreal.Vector(target['x'] * 100, target['y'] * 100 + 7, 0), False, False)
replace_tags(shelf, 'TEAM_V3_SHELF')
if not unreal.EditorLevelLibrary.save_current_level():
    raise RuntimeError('could not save the isolated team-slot level')
if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
    raise RuntimeError('could not save external team-slot actors')
unreal.log('TEAM_V3_SLOT_PILOT_SAVED red-slots=1,2,3 shelf=N02')
