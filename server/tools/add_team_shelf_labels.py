"""Add visual SKU labels only to an explicitly selected disposable UE scene.

Letters move with the synthetic source products; colored shelf labels identify
their destinations. The labels do not identify real products or measured stock.
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
if os.lstat(str(project / 'Content')).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    raise RuntimeError('Content must not be a junction')
world = json.loads((project / 'Content/Data/warehouse_map.json').read_text(encoding='utf-8'))
if world.get('map_id') != 'mat_ssafy_v3':
    raise RuntimeError('matching v3 map required')
unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/AGV/NewMap')
editor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = editor.get_all_level_actors()
if any('TEAM_V3_ITEM_LABEL' in {str(t) for t in a.get_editor_property('tags')} for a in actors):
    raise RuntimeError('this one-time label setup was already run')

products = [a for a in actors if a.get_class().get_name().startswith(('BP_RedObject_', 'BP_BlueObject_'))]
shelves = [a for a in actors if a.get_class().get_name().startswith(('BP_RedShelf_', 'BP_BlueShelf_'))]
# Dark saturated tones stay legible against the bright warehouse floor.
specs = [('milk', 1, 'N02', 'M', 'N02 MILK', unreal.ProductType.RED, unreal.Color(20, 20, 90, 255)),
         ('tissue', 2, 'N03', 'T', 'N03 TISSUE', unreal.ProductType.BLUE, unreal.Color(90, 30, 10, 255)),
         ('egg', 3, 'N04', 'E', 'N04 EGG', unreal.ProductType.GREEN, unreal.Color(70, 10, 70, 255)),
         ('juice', 4, 'N05', 'J', 'N05 JUICE', unreal.ProductType.TEAM_JUICE, unreal.Color(0, 70, 20, 255))]


def one(items, predicate, description):
    matches = [item for item in items if predicate(item)]
    if len(matches) != 1:
        raise RuntimeError('expected exactly one ' + description)
    return matches[0]


def label(parent, product, text, color, z, size, attach):
    position = parent.get_actor_location()
    position.z += z
    # Face the v3 demo camera (yaw -90, pitch -55) so the labels read
    # horizontally instead of lying vertically in the rendered view.
    marker = editor.spawn_actor_from_class(unreal.TextRenderActor, position, unreal.Rotator(0, 55, 90))
    if not marker:
        raise RuntimeError('failed to create simulated item label')
    marker.set_actor_label('V3_LABEL_{}_{}'.format(product.upper(), 'ITEM' if attach else 'SHELF'))
    marker.set_editor_property('tags', [unreal.Name('TEAM_V3_ITEM_LABEL'),
                                        unreal.Name('TEAM_V3_LABEL_' + product.upper())])
    component = marker.text_render
    component.set_text(unreal.Text(text))
    component.set_text_render_color(color)
    component.set_world_size(size)
    if attach:
        marker.attach_to_actor(parent, '', unreal.AttachmentRule.KEEP_WORLD,
                               unreal.AttachmentRule.KEEP_WORLD, unreal.AttachmentRule.KEEP_WORLD, False)
        if marker.get_attach_parent_actor() != parent:
            raise RuntimeError('could not attach label to synthetic item')


resolved = []
for product, slot, shelf_id, letter, shelf_text, product_type, color in specs:
    item = one(products, lambda a: 'TEAM_V3_SLOT_{}'.format(slot) in
               {str(t) for t in a.get_editor_property('tags')}, 'source slot ' + str(slot))
    shelf = one(shelves, lambda a: str(a.get_editor_property('shelf_id')) == shelf_id, 'shelf ' + shelf_id)
    if (item.get_editor_property('product_type') != product_type or
            shelf.get_editor_property('shelf_product_type') != product_type or
            'TEAM_V3_SHELF' not in {str(t) for t in shelf.get_editor_property('tags')}):
        raise RuntimeError('simulated slot and shelf product do not match: ' + product)
    resolved.append((product, letter, shelf_text, color, item, shelf))

for product, letter, shelf_text, color, item, shelf in resolved:
    label(item, product, letter, color, 9, 6.0, True)
    label(shelf, product, shelf_text, color, 15, 7.0, False)

if not unreal.EditorLevelLibrary.save_current_level():
    raise RuntimeError('failed to save synthetic labels')
if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
    raise RuntimeError('failed to save synthetic label actors')
unreal.log('TEAM_SHELF_LABELS_SAVED products=4 labels=8')
