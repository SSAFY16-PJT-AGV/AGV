"""Verify synthetic team labels on an already prepared independent UE scene."""
from pathlib import Path
import os

import unreal

project = Path(unreal.Paths.project_dir()).resolve()
selected = os.environ.get('AGV_TEAM_FULL_PROJECT', '')
if os.environ.get('AGV_TEAM_FULL_CONFIRM') != 'ISOLATED' or not selected or project != Path(selected).resolve():
    raise RuntimeError('explicit matching disposable team-shelf project required')
unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/AGV/NewMap')
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
labels = [a for a in actors if isinstance(a, unreal.TextRenderActor) and
          'TEAM_V3_ITEM_LABEL' in {str(t) for t in a.get_editor_property('tags')}]
expected = {'milk': (1, 'M', 'N02 MILK'), 'tissue': (2, 'T', 'N03 TISSUE'),
            'egg': (3, 'E', 'N04 EGG'), 'juice': (4, 'J', 'N05 JUICE')}
for product, (slot, item_text, shelf_text) in expected.items():
    selected = [a for a in labels if 'TEAM_V3_LABEL_' + product.upper() in
                {str(t) for t in a.get_editor_property('tags')}]
    if len(selected) != 2 or {str(a.text_render.get_editor_property('text')) for a in selected} != {item_text, shelf_text}:
        raise RuntimeError('two matching labels required for ' + product)
    linked = [a for a in selected if str(a.text_render.get_editor_property('text')) == item_text]
    parent = linked[0].get_attach_parent_actor() if len(linked) == 1 else None
    if parent is None or 'TEAM_V3_SLOT_' + str(slot) not in {str(t) for t in parent.get_editor_property('tags')}:
        raise RuntimeError('moving item label must be attached to its item: ' + product)
if len(labels) != 8:
    raise RuntimeError('unexpected synthetic label count')
unreal.log('TEAM_SHELF_LABELS_VERIFIED products=4 labels=8')
