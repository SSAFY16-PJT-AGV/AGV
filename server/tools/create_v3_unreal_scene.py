"""Create an isolated v3 demo scene in an explicit disposable UE project.

Run with Unreal's Editor Python, never with the team's original project. The
server-supplied path is only a synthetic projection: its object locations and
parking assumptions are not physical inventory or safety evidence.

Set AGV_V3_SCENE_PROJECT to the absolute disposable project directory and
AGV_V3_SCENE_CONFIRM=ISOLATED before launching UnrealEditor-Cmd with
-ExecutePythonScript=<this file>. The target Content must not be a junction.
"""
import json
import math
import os
from pathlib import Path
import stat

import unreal


project = Path(unreal.Paths.project_dir()).resolve()
requested = os.environ.get('AGV_V3_SCENE_PROJECT', '')
if os.environ.get('AGV_V3_SCENE_CONFIRM') != 'ISOLATED' or not requested:
    raise RuntimeError('explicit disposable project and isolated-scene confirmation required')
if project != Path(requested).resolve():
    raise RuntimeError('running Unreal project is not the explicitly selected disposable copy')
content = project / 'Content'
if os.lstat(str(content)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    raise RuntimeError('Content must be an independent directory, not a junction')

# This optional arrangement is used only for deterministic virtual-reroute
# tests, with the matching C++ guard patch and simulated start configuration.
variant = os.environ.get('AGV_V3_SCENE_VARIANT', 'standard')
if variant not in ('standard', 'reroute'):
    raise RuntimeError('unknown isolated v3 scene variant')
pickup_nodes = ('N01', 'N04') if variant == 'reroute' else ('D01', 'D06')
shelf_nodes = ('D01', 'D04') if variant == 'reroute' else ('D04', 'D03')
robot_nodes = ('N05', 'J02') if variant == 'reroute' else ('D07', 'D08')

source = Path(__file__).resolve().parents[1] / 'examples' / 'mat_ssafy_v3_unreal_sim.json'
world_map = json.loads(source.read_text(encoding='utf-8'))
if world_map.get('map_id') != 'mat_ssafy_v3' or world_map.get('coordinate_unit') != 'meter':
    raise RuntimeError('expected an explicit v3 projection')
nodes = {row['id']: row for row in world_map['nodes']}
if not {'D01', 'D03', 'D04', 'D06', 'D07', 'D08'}.issubset(nodes):
    raise RuntimeError('the v3 projection is missing demo nodes')
target_map = content / 'Data' / 'warehouse_map.json'
target_map.write_text(json.dumps(world_map, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

# The team node marker is a 100 cm cube with component scale (.2,.2,.05):
# 20 cm wide on a v3 map only about 200 cm across. In this isolated copy,
# shrink the existing Blueprint component to 2 cm; do not change the team
# source project or its marker defaults for the original large warehouse.
marker_bp = unreal.EditorAssetLibrary.load_asset('/Game/Blueprints/BP_NodeMarker')
if not marker_bp:
    raise RuntimeError('team node marker Blueprint is missing')
subobjects = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
helpers = unreal.SubobjectDataBlueprintFunctionLibrary
marker_components = {}
for handle in subobjects.k2_gather_subobject_data_for_blueprint(marker_bp):
    data = helpers.get_data(handle)
    obj = helpers.get_object_for_blueprint(data, marker_bp)
    if obj and obj.get_class().get_name() == 'StaticMeshComponent' and obj.get_name().startswith('nodeMesh'):
        marker_components[obj.get_path_name()] = obj
if len(marker_components) != 1:
    raise RuntimeError('expected one node marker mesh template')
marker_mesh = next(iter(marker_components.values()))
marker_mesh.set_editor_property('relative_scale3d', unreal.Vector(.02, .02, .005))
if not unreal.EditorAssetLibrary.save_loaded_asset(marker_bp, False):
    raise RuntimeError('failed to save the isolated node marker Blueprint')
unreal.log('AGV_V3_NODE_MARKER_SAVED scale=(0.02,0.02,0.005)')

levels = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
levels.load_level('/Game/AGV/NewMap')
all_actors = actors.get_all_level_actors()

def named_class(prefix):
    return [actor for actor in all_actors if actor.get_class().get_name().startswith(prefix)]

red_shelves = named_class('BP_RedShelf_')
blue_shelves = named_class('BP_BlueShelf_')
cameras = named_class('BP_WarehouseCameraPawn_')
robots = named_class('BP_AGVRobot_')
starts = named_class('PlayerStart')
red_products = sorted(named_class('BP_RedObject_'), key=lambda actor: actor.get_actor_label())
blue_products = sorted(named_class('BP_BlueObject_'), key=lambda actor: actor.get_actor_label())
if len(red_shelves) != 1 or len(blue_shelves) != 1 or len(cameras) != 1 or not robots:
    raise RuntimeError('unexpected team scene: expected red/blue shelves, one camera and a robot')
if len(red_products) < 3 or len(blue_products) < 3:
    raise RuntimeError('expected at least three existing products of each color')

def xy(node):
    return nodes[node]['x'] * 100.0, nodes[node]['y'] * 100.0

def move_position(actor, x, y, z):
    if hasattr(actor, 'modify'):
        actor.modify()
    root = actor.get_editor_property('root_component')
    if root and hasattr(root, 'modify'):
        root.modify()
    result = actor.set_actor_location(unreal.Vector(x, y, z), False, False)
    actual = actor.get_actor_location()
    unreal.log('AGV_V3_MOVE {} returned={} actual=({},{},{})'.format(
        actor.get_actor_label(), result, actual.x, actual.y, actual.z))
    if any(not math.isclose(value, target, abs_tol=0.01)
           for value, target in zip((actual.x, actual.y, actual.z), (x, y, z))):
        raise RuntimeError('actor move failed: ' + actor.get_actor_label())

def move(actor, node, z=0.0, dy=0.0):
    x, y = xy(node)
    move_position(actor, x, y + dy, z)

def scale(actor, value):
    actor.set_actor_scale3d(unreal.Vector(value, value, value))
    actual = actor.get_actor_scale3d()
    unreal.log('AGV_V3_SCALE {} actual=({},{},{})'.format(actor.get_actor_label(), actual.x, actual.y, actual.z))
    if any(not math.isclose(v, value, abs_tol=0.001) for v in (actual.x, actual.y, actual.z)):
        raise RuntimeError('actor scaling failed: ' + actor.get_actor_label())

def mark_actor_edited(actor):
    # World Partition stores these instances in separate actor packages. The
    # runtime transform setters alone need not dirty those packages in Editor.
    # Alternate the private demo marker so repeating the script also changes a
    # property and persists the transforms, even on an already prepared copy.
    tags = list(actor.get_editor_property('tags'))
    marker_a, marker_b = unreal.Name('AGV_V3_ISOLATED_A'), unreal.Name('AGV_V3_ISOLATED_B')
    next_marker = marker_b if marker_a in tags else marker_a
    tags = [tag for tag in tags if tag not in (marker_a, marker_b, unreal.Name('AGV_V3_ISOLATED'))]
    tags.append(next_marker)
    actor.set_editor_property('tags', tags)

# Keep existing product actors; spawning new actors in the team's partitioned
# level can enter the editor actor factory and fail before the scene is saved.
# Remove extra products and previously baked v2 paths only from this copy.
# Paths for v3 are spawned by the existing map loader's BeginPlay.
kept_products = set(red_products[:3] + blue_products[:3])
legacy_props = [actor for actor in all_actors if actor.get_class().get_name() == 'StaticMeshActor'
                and not actor.get_actor_label().startswith('SM_Floor')
                and actor.get_actor_label() != 'SM_SkySphere']
unreal.log('AGV_V3_SCENE_REMOVING_OBSOLETE_ACTORS')
for actor in all_actors:
    class_name = actor.get_class().get_name()
    old_product = class_name.startswith(('BP_RedObject_', 'BP_BlueObject_', 'BP_GreenObject_'))
    if (old_product and actor not in kept_products) or class_name == 'AGVPathActor':
        if not actors.destroy_actor(actor):
            raise RuntimeError('could not remove obsolete isolated-scene actor')

# The legacy warehouse spans roughly 16 m, while v3 spans roughly 2 m. Large
# walls, containers and tanks can fill the camera view and conceal the new
# route. Hide these legacy props only in the disposable v3 scene, preserving
# the original floor and sky actors. They remain present for easy inspection.
unreal.log('AGV_V3_SCENE_HIDING_LEGACY_PROPS')
for actor in legacy_props:
    actor.set_actor_hidden_in_game(True)
    mark_actor_edited(actor)

unreal.log('AGV_V3_SCENE_PLACING_SHELVES')
for shelf, node, offset in ((red_shelves[0], shelf_nodes[0], -7.0), (blue_shelves[0], shelf_nodes[1], 7.0)):
    move(shelf, node, dy=offset)
    scale(shelf, 0.1)
    shelf.set_editor_property('initial_occupied_slots', [])
    mark_actor_edited(shelf)

unreal.log('AGV_V3_SCENE_PLACING_PRODUCTS')
for items, node in ((red_products[:3], pickup_nodes[0]), (blue_products[:3], pickup_nodes[1])):
    x, y = xy(node)
    for product, dx in zip(items, (-5.0, 0.0, 5.0)):
        move_position(product, x + dx, y, 2.0)
        scale(product, .08)
        mark_actor_edited(product)

unreal.log('AGV_V3_SCENE_PLACING_ROBOTS')
for robot in robots:
    robot_id = str(robot.get_editor_property('agv_id'))
    if robot_id not in ('AGV_01', 'AGV_02'):
        raise RuntimeError('unexpected robot identifier')
    move(robot, robot_nodes[0] if robot_id == 'AGV_01' else robot_nodes[1], z=3.0)
    scale(robot, .1)
    mark_actor_edited(robot)

if len(starts) != 1:
    raise RuntimeError('expected one team player start for the spawned pawn')
move(starts[0], robot_nodes[0], z=3.0)
mark_actor_edited(starts[0])

camera = cameras[0]
move_position(camera, 100.0, 50.0, 0.0)
arm = camera.get_component_by_class(unreal.SpringArmComponent)
if not arm:
    raise RuntimeError('team camera has no spring arm')
arm.set_editor_property('target_arm_length', 360.0)

unreal.log('AGV_V3_SCENE_SAVING')
if not unreal.EditorLevelLibrary.save_current_level():
    raise RuntimeError('failed to save isolated v3 level')
if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
    raise RuntimeError('failed to save isolated v3 external actor packages')
unreal.log('AGV_V3_ISOLATED_SCENE_SAVED')
