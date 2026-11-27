"""Read-only saved-editor validation of the isolated six-shelf demo scene."""
import json
import math
from pathlib import Path

import unreal


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


project = Path(unreal.Paths.project_dir()).resolve()
world = json.loads((project / 'Content/Data/warehouse_map.json').read_text(encoding='utf-8'))
require(world.get('map_id') == 'mat_ssafy_v3', 'matching v3 map required')
nodes = {node['id']: node for node in world['nodes']}
unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/AGV/NewMap')
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
shelves = [a for a in actors if a.get_class().get_name().startswith(('BP_RedShelf_', 'BP_BlueShelf_'))]
products = [a for a in actors if a.get_class().get_name().startswith(('BP_RedObject_', 'BP_BlueObject_'))]
require(len(shelves) == 6 and len(products) == 4, 'six shelves and four source objects are required')
expected = [unreal.ProductType.RED, unreal.ProductType.RED, unreal.ProductType.BLUE,
            unreal.ProductType.GREEN, unreal.ProductType.TEAM_JUICE, unreal.ProductType.RED]
for index, product_type in enumerate(expected, start=1):
    shelf_id = 'N{:02d}'.format(index)
    selected = [shelf for shelf in shelves if str(shelf.get_editor_property('shelf_id')) == shelf_id]
    require(len(selected) == 1, 'one {} shelf required'.format(shelf_id))
    shelf = selected[0]
    tags = {str(t) for t in shelf.get_editor_property('tags')}
    require({'TEAM_V3_SHELF', 'TEAM_V3_SHELF_' + shelf_id} <= tags, 'missing shelf tags')
    require(shelf.get_editor_property('shelf_product_type') == product_type, 'shelf SKU mismatch')
    node, location = nodes['D{:02d}'.format(index)], shelf.get_actor_location()
    require(math.hypot(location.x - node['x'] * 100, location.y - node['y'] * 100) <= 10,
            'shelf position mismatch')
for index, product_type in enumerate(expected[1:5], start=1):
    found = [a for a in products if 'TEAM_V3_SLOT_{}'.format(index) in
             {str(tag) for tag in a.get_editor_property('tags')}]
    require(len(found) == 1 and found[0].get_editor_property('product_type') == product_type,
            'source slot or product mismatch')
    position, source = found[0].get_actor_location(), nodes['D01']
    require(math.hypot(position.x - source['x'] * 100, position.y - source['y'] * 100) <= 12,
            'source item is not near D01')
unreal.log('TEAM_FULL_SCENE_VERIFIED shelves=6 source-slots=4')
