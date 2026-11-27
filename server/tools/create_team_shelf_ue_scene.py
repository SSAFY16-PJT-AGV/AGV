"""Create six four-slot shelves in a disposable v3 Unreal demonstration copy.

Milk/tissue/egg/juice use pre-existing demo meshes as temporary visual proxies;
the mapping and stock are simulated and do not claim measured warehouse state.
"""
import json
import os
from pathlib import Path
import stat

import unreal


project = Path(unreal.Paths.project_dir()).resolve()
selected = os.environ.get('AGV_TEAM_FULL_PROJECT', '')
if os.environ.get('AGV_TEAM_FULL_CONFIRM') != 'ISOLATED' or not selected or project != Path(selected).resolve():
    raise RuntimeError('explicit matching disposable team-shelf project required')
content = project / 'Content'
if os.lstat(str(content)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    raise RuntimeError('Content must not be a junction')
world = json.loads((content / 'Data/warehouse_map.json').read_text(encoding='utf-8'))
if world.get('map_id') != 'mat_ssafy_v3' or world.get('coordinate_unit') != 'meter':
    raise RuntimeError('explicit v3 world required')
nodes = {node['id']: node for node in world['nodes']}
levels = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
levels.load_level('/Game/AGV/NewMap')
all_actors = actors.get_all_level_actors()
red = sorted((a for a in all_actors if a.get_class().get_name().startswith('BP_RedObject_')),
             key=lambda actor: actor.get_actor_label())
blue = sorted((a for a in all_actors if a.get_class().get_name().startswith('BP_BlueObject_')),
              key=lambda actor: actor.get_actor_label())
shelves = [a for a in all_actors if a.get_class().get_name().startswith(('BP_RedShelf_', 'BP_BlueShelf_'))]
if len(red) != 3 or len(blue) != 3 or len(shelves) != 2:
    raise RuntimeError('one-time setup requires an unmodified v3 pilot copy')


def mark(actor, tags):
    existing = [tag for tag in actor.get_editor_property('tags')
                if not str(tag).startswith('TEAM_V3_SLOT_') and not str(tag).startswith('TEAM_V3_SHELF')]
    rev_a, rev_b = unreal.Name('TEAM_FULL_REV_A'), unreal.Name('TEAM_FULL_REV_B')
    next_rev = rev_b if rev_a in existing else rev_a
    existing = [tag for tag in existing if tag not in (rev_a, rev_b)]
    actor.set_editor_property('tags', existing + [unreal.Name(tag) for tag in tags] + [next_rev])


unreal.log('TEAM_FULL_DUPLICATING_SHELVES')
for _ in range(4):
    clone = actors.duplicate_actor(shelves[0])
    if not clone:
        raise RuntimeError('failed to duplicate a demo shelf')
    shelves.append(clone)

types = [unreal.ProductType.RED, unreal.ProductType.RED, unreal.ProductType.BLUE,
         unreal.ProductType.GREEN, unreal.ProductType.TEAM_JUICE, unreal.ProductType.RED]
for index, (shelf, product_type) in enumerate(zip(shelves, types), start=1):
    node_id, shelf_id = 'D{:02d}'.format(index), 'N{:02d}'.format(index)
    node = nodes[node_id]
    y_offset = -7 if node['y'] < .5 else 7
    shelf.set_actor_location(unreal.Vector(node['x'] * 100, node['y'] * 100 + y_offset, 0), False, False)
    shelf.set_editor_property('shelf_id', shelf_id)
    shelf.set_editor_property('shelf_product_type', product_type)
    shelf.set_editor_property('initial_occupied_slots', [])
    shelf.set_actor_scale3d(unreal.Vector(.1, .1, .1))
    mark(shelf, ['TEAM_V3_SHELF', 'TEAM_V3_SHELF_' + shelf_id])
    shelf.set_actor_label('V3_{}'.format(shelf_id))

# Four independent source slot tags, one sample of each team product. Existing
# meshes are deliberate proxies; their colour is not used for SKU identification.
chosen = [red[0], blue[0], red[1], blue[1]]
for obsolete in (red[2], blue[2]):
    if not actors.destroy_actor(obsolete):
        raise RuntimeError('could not remove extra pilot item')
source = nodes['D01']
for index, (item, product_type) in enumerate(zip(chosen, types[1:5]), start=1):
    item.set_actor_location(unreal.Vector(source['x'] * 100 + (index - 2.5) * 5,
                                          source['y'] * 100, 2), False, False)
    item.set_editor_property('product_type', product_type)
    item.set_actor_scale3d(unreal.Vector(.08, .08, .08))
    mark(item, ['TEAM_V3_SLOT_{}'.format(index)])
    item.set_actor_label('V3_source_slot_{}'.format(index))

if not unreal.EditorLevelLibrary.save_current_level():
    raise RuntimeError('failed to save six-shelf level')
if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
    raise RuntimeError('failed to save six-shelf actor packages')
unreal.log('TEAM_FULL_SCENE_SAVED shelves=6 source-slots=4')
