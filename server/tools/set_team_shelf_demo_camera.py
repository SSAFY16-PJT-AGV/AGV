"""Set a closer, user-adjustable camera on the isolated six-shelf demo copy."""
import os
from pathlib import Path

import unreal

project = Path(unreal.Paths.project_dir()).resolve()
selected = os.environ.get('AGV_TEAM_FULL_PROJECT', '')
if os.environ.get('AGV_TEAM_FULL_CONFIRM') != 'ISOLATED' or not selected or project != Path(selected).resolve():
    raise RuntimeError('explicit matching disposable team-shelf project required')
unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/AGV/NewMap')
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
cameras = [a for a in actors if isinstance(a, unreal.WarehouseCameraPawn)]
if len(cameras) != 1:
    raise RuntimeError('expected exactly one warehouse camera')
arms = cameras[0].get_components_by_class(unreal.SpringArmComponent)
if len(arms) != 1:
    raise RuntimeError('expected one camera spring arm')
arms[0].set_editor_property('target_arm_length', 425.0)
tags = [tag for tag in cameras[0].get_editor_property('tags') if str(tag) != 'TEAM_FULL_CAMERA']
cameras[0].set_editor_property('tags', tags + [unreal.Name('TEAM_FULL_CAMERA')])
if not unreal.EditorLevelLibrary.save_current_level():
    raise RuntimeError('failed to save camera level')
if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
    raise RuntimeError('failed to save camera actor')
unreal.log('TEAM_FULL_CAMERA_SAVED arm=425')
