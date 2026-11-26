"""Read-only UE Editor check for an isolated v3 scene.

Run this with ``UnrealEditor-Cmd <project> -nullrhi -unattended
-ExecutePythonScript=<this script>`` after creating the demo scene.  Check the
editor log for AGV_V3_SCENE_VERIFIED: Unreal can exit zero after a Python
exception.  This checks saved editor data, not gameplay, actual positions or
physical safety.  It never writes the project or the loaded level.
"""
import json
import math
from pathlib import Path

import unreal


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


project = Path(unreal.Paths.project_dir()).resolve()
source = Path(__file__).resolve().parents[1] / 'examples' / 'mat_ssafy_v3_unreal_sim.json'
expected_map = json.loads(source.read_text(encoding='utf-8'))
actual_map = json.loads((project / 'Content' / 'Data' / 'warehouse_map.json').read_text(encoding='utf-8'))
require(actual_map == expected_map and actual_map.get('map_id') == 'mat_ssafy_v3',
        'the loaded project does not contain the expected synthetic v3 projection')

marker_bp = unreal.EditorAssetLibrary.load_asset('/Game/Blueprints/BP_NodeMarker')
require(bool(marker_bp), 'team node marker Blueprint is missing')
subobjects = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
helpers = unreal.SubobjectDataBlueprintFunctionLibrary
marker_components = {}
for handle in subobjects.k2_gather_subobject_data_for_blueprint(marker_bp):
    obj = helpers.get_object_for_blueprint(helpers.get_data(handle), marker_bp)
    if obj and obj.get_class().get_name() == 'StaticMeshComponent' and obj.get_name().startswith('nodeMesh'):
        marker_components[obj.get_path_name()] = obj
require(len(marker_components) == 1, 'expected one node marker mesh template')
marker_scale = next(iter(marker_components.values())).get_editor_property('relative_scale3d')
require(all(math.isclose(value, target, abs_tol=.0001)
            for value, target in zip((marker_scale.x, marker_scale.y, marker_scale.z), (.02, .02, .005))),
        'v3 node marker must be 2 cm wide')

levels = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
levels.load_level('/Game/AGV/NewMap')
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()


def classes(prefix):
    return [actor for actor in actors if actor.get_class().get_name().startswith(prefix)]


def near(actual, expected):
    return all(math.isclose(value, target, abs_tol=.01)
               for value, target in zip((actual.x, actual.y, actual.z), expected))


def one(prefix):
    matches = classes(prefix)
    require(len(matches) == 1, '{}: expected one, found {}'.format(prefix, len(matches)))
    return matches[0]


nodes = {node['id']: node for node in actual_map['nodes']}


def position(node, z=0.0, dx=0.0, dy=0.0):
    row = nodes[node]
    return (row['x'] * 100.0 + dx, row['y'] * 100.0 + dy, z)


for color, node in (('Red', 'D01'), ('Blue', 'D06')):
    products = classes('BP_{}Object_'.format(color))
    require(len(products) == 3, '{} pickup must contain three demo products'.format(color))
    actual = sorted((actor.get_actor_location().x, actor.get_actor_location().y,
                     actor.get_actor_location().z) for actor in products)
    expected = sorted(position(node, z=2.0, dx=offset) for offset in (-5.0, 0.0, 5.0))
    require(all(all(math.isclose(a, b, abs_tol=.01) for a, b in zip(point, target))
                for point, target in zip(actual, expected)), color + ' pickup position mismatch')
    for actor in products:
        require(near(actor.get_actor_scale3d(), (.08, .08, .08)), color + ' product scale mismatch')
require(not classes('BP_GreenObject_'), 'unexpected green demo pickup')

for color, node, offset in (('Red', 'D04', -7.0), ('Blue', 'D03', 7.0)):
    shelf = one('BP_{}Shelf_'.format(color))
    require(str(shelf.get_editor_property('shelf_id')) == 'SHELF_' + color.upper(), 'shelf ID mismatch')
    require(near(shelf.get_actor_location(), position(node, dy=offset)), color + ' shelf position mismatch')
    require(near(shelf.get_actor_scale3d(), (.1, .1, .1)), color + ' shelf scale mismatch')

robot = one('BP_AGVRobot_')
require(str(robot.get_editor_property('agv_id')) == 'AGV_02', 'expected placed AGV_02')
require(near(robot.get_actor_location(), position('D08', z=3.0)), 'AGV_02 start mismatch')
require(near(robot.get_actor_scale3d(), (.1, .1, .1)), 'AGV_02 scale mismatch')
require(near(one('PlayerStart').get_actor_location(), position('D07', z=3.0)), 'AGV_01 start mismatch')

camera = one('BP_WarehouseCameraPawn_')
require(near(camera.get_actor_location(), (100.0, 50.0, 0.0)), 'camera center mismatch')
arm = camera.get_component_by_class(unreal.SpringArmComponent)
require(arm and math.isclose(arm.get_editor_property('target_arm_length'), 360.0, abs_tol=.01),
        'camera arm mismatch')
one('BP_WarehouseMapLoader_')
require(not classes('AGVPathActor'), 'obsolete paths remain baked into the level')
props = classes('StaticMeshActor')
for actor in props:
    label = actor.get_actor_label()
    expected_hidden = not label.startswith('SM_Floor') and label != 'SM_SkySphere'
    require(bool(actor.get_editor_property('hidden')) == expected_hidden,
            'legacy prop visibility mismatch: ' + label)

unreal.log('AGV_V3_SCENE_VERIFIED: red=3 blue=3 shelves=2 robot=1 '
           'player-start=1 camera=1 baked-paths=0 marker=2cm hidden-props={}'.format(
               sum(bool(actor.get_editor_property('hidden')) for actor in props)))
