"""FSL static mesh batch FBX exporter. Source objects and node groups stay editable."""
bl_info = {
    'name': 'FSL 批量导出', 'author': 'Procedural Forest Assets',
    'version': (1, 0, 0), 'blender': (4, 2, 0),
    'location': '3D View > Sidebar > FSL',
    'description': '检查所选/集合网格，将GN结果逐件导出FBX及JSON，保护源对象和已有文件',
    'category': 'Import-Export',
}

import bpy
import json
import math
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timezone
from mathutils import Matrix
from bpy.props import StringProperty, BoolProperty, EnumProperty, PointerProperty, CollectionProperty

VERSION = '1.0.0'


def file_stem(name, prefix='SM_'):
    """Replace a GN_ prefix, otherwise add prefix once; sanitize for Windows paths."""
    base = name[3:] if name.startswith('GN_') else name
    if prefix and not base.startswith(prefix):
        base = prefix + base
    base = re.sub(r'[^A-Za-z0-9_\-\u4e00-\u9fff]+', '_', base).strip('_')
    if not base:
        raise ValueError('对象名称无法生成有效文件名')
    if len(base) > 120:
        raise ValueError('对象名称太长，请缩短后导出')
    if base.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}:
        base = '_' + base
    return base


def targets(context, settings):
    if settings.source_mode == 'COLLECTION':
        if not settings.source_collection:
            raise ValueError('先选择要导出的集合')
        candidates = list(settings.source_collection.all_objects)
    else:
        candidates = list(context.selected_objects)
    if settings.name_filter:
        candidates = [o for o in candidates if o.name.startswith(settings.name_filter)]
    return sorted(candidates, key=lambda o: o.name.casefold())


def scalar_parameters(obj):
    result = []
    for mod in obj.modifiers:
        if mod.type != 'NODES' or mod.node_group is None:
            continue
        values = {}
        for item in mod.node_group.interface.items_tree:
            if item.item_type != 'SOCKET' or item.in_out != 'INPUT':
                continue
            try:
                inputs = getattr(getattr(mod, 'properties', None), 'inputs', None)
                value = getattr(inputs, item.identifier).value if inputs is not None else mod.get(item.identifier)
                if isinstance(value, (int, float, bool, str)):
                    values[item.name] = value
            except (AttributeError, TypeError, KeyError):
                continue
        result.append({'modifier': mod.name, 'node_group': mod.node_group.name, 'inputs': values})
    return result


def mesh_info(mesh, unit_scale):
    if not mesh.vertices or not mesh.polygons:
        raise ValueError('求值结果没有可导出的网格面')
    mesh.calc_loop_triangles()
    coords = [tuple(v.co) for v in mesh.vertices]
    if any(not math.isfinite(c) for p in coords for c in p):
        raise ValueError('顶点坐标含无效数值')
    lo = [min(v[i] for v in coords) for i in range(3)]
    hi = [max(v[i] for v in coords) for i in range(3)]
    wind = mesh.color_attributes.get('WindMask')
    values = [c.color[0] for c in wind.data] if wind else []
    return {
        'vertices': len(mesh.vertices), 'triangles': len(mesh.loop_triangles),
        'dimensions_m': [(hi[i] - lo[i]) * unit_scale for i in range(3)],
        'bounds_local': [lo, hi], 'uv_layers': [x.name for x in mesh.uv_layers],
        'materials': [m.name if m else None for m in mesh.materials],
        'color_attributes': [a.name for a in mesh.color_attributes],
        'wind_range': [min(values), max(values)] if values else None,
    }


def _realize_group():
    g = bpy.data.node_groups.new('.FSL_Export_Realize_' + uuid.uuid4().hex[:8], 'GeometryNodeTree')
    g.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    g.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    inp = g.nodes.new('NodeGroupInput')
    real = g.nodes.new('GeometryNodeRealizeInstances')
    out = g.nodes.new('NodeGroupOutput')
    g.links.new(inp.outputs['Geometry'], real.inputs['Geometry'])
    g.links.new(real.outputs['Geometry'], out.inputs['Geometry'])
    return g


def _snapshot(context, source, collection, group):
    clone = None
    mesh = None
    try:
        clone = source.copy()
        clone.name = '.FSL_Evaluate_' + source.name
        world = source.matrix_world.copy()
        clone.parent = None
        clone.matrix_world = world
        collection.objects.link(clone)
        clone.hide_viewport = False
        clone.hide_render = False
        clone.hide_set(False)
        last = clone.modifiers.new('.FSL_Realize', 'NODES')
        last.node_group = group
        context.view_layer.update()
        dg = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(clone.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
        if mesh is None:
            raise ValueError('无法取得求值网格')
        # Bake world rotation/scale, omit translation: retain the source pivot at file origin.
        basis = world.to_3x3()
        if basis.determinant() <= 1e-10:
            raise ValueError('检测到镜像或零缩放；请先在副本中处理变换')
        mesh.transform(basis.to_4x4())
        mesh.update()
        wind = mesh.color_attributes.get('WindMask')
        if wind:
            index = mesh.color_attributes.find('WindMask')
            mesh.color_attributes.active_color_index = index
            mesh.color_attributes.render_color_index = index
        return mesh
    except Exception:
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise
    finally:
        if clone:
            bpy.data.objects.remove(clone, do_unlink=True)


def _publish_new(source, destination, created):
    # Exclusive create protects both existing outputs and races with another export.
    with destination.open('xb') as target:
        created.append(destination)
        with source.open('rb') as inp:
            shutil.copyfileobj(inp, target)


def run_batch(context, objects, output_dir, prefix='SM_', require_wind=False, check_only=False):
    if context.mode != 'OBJECT':
        raise ValueError('请先切换到物体模式')
    objects = list(objects)
    if not objects:
        raise ValueError('没有符合条件的对象；检查集合、选择与名称前缀')
    root = Path(bpy.path.abspath(str(output_dir))).expanduser().resolve()
    if not str(output_dir).strip():
        raise ValueError('请先填写输出文件夹')
    if len(str(root)) > 180:
        raise ValueError('输出路径太长，请选更短的文件夹')
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    report_path = root / ('FSL_BatchExport_Report_' + run_id + '.json')
    report = {
        'schema_version': 1, 'tool': 'FSL Batch Exporter', 'version': VERSION,
        'created_utc': datetime.now(timezone.utc).isoformat(), 'blender': bpy.app.version_string,
        'source_file': bpy.data.filepath, 'source_has_unsaved_changes': bpy.data.is_dirty,
        'scene': context.scene.name, 'frame': context.scene.frame_current,
        'output_dir': str(root), 'mode': 'CHECK' if check_only else 'EXPORT',
        'transform_policy': 'Bake world rotation/scale; remove translation; retain source pivot',
        'evaluation': 'Current viewport modifier evaluation, realizes instances on temporary copy',
        'unit_scale_length': context.scene.unit_settings.scale_length,
        'require_WindMask': require_wind, 'items': [], 'ue_validated': False,
    }
    names = {}
    errors = {}
    for o in objects:
        try:
            stem = file_stem(o.name, prefix)
            names.setdefault(stem.casefold(), []).append(o.name)
        except ValueError as e:
            errors[o.name] = str(e)
    for matching in names.values():
        if len(matching) > 1:
            for name in matching:
                errors[name] = '导出文件名冲突：' + ', '.join(matching)

    original_selection = list(context.selected_objects)
    original_active = context.view_layer.objects.active
    collection = bpy.data.collections.new('.FSL_Export_Temp_' + uuid.uuid4().hex[:8])
    context.scene.collection.children.link(collection)
    group = None
    try:
        group = _realize_group()
        for obj in objects:
            item = {'object': obj.name, 'status': 'FAILED', 'warnings': [], 'error': None}
            report['items'].append(item)
            mesh = None
            temp_obj = None
            created = []
            try:
                if obj.name in errors:
                    raise ValueError(errors[obj.name])
                if obj.type != 'MESH':
                    raise ValueError('首版只处理静态网格对象')
                if obj.name not in context.view_layer.objects or not obj.visible_get(view_layer=context.view_layer):
                    raise ValueError('对象在当前视图层不可见；避免导出非预期求值结果')
                stem = file_stem(obj.name, prefix)
                fbx = root / (stem + '.fbx')
                sidecar = root / (stem + '.json')
                item['fbx'] = str(fbx)
                item['record'] = str(sidecar)
                if fbx.exists() or sidecar.exists():
                    raise FileExistsError('同名FBX或JSON已存在，已阻止覆盖；换输出文件夹后重试')
                item['source_matrix_world'] = [list(row) for row in obj.matrix_world]
                item['gn_parameters'] = scalar_parameters(obj)
                mesh = _snapshot(context, obj, collection, group)
                item['mesh'] = mesh_info(mesh, context.scene.unit_settings.scale_length)
                if not item['mesh']['uv_layers']:
                    item['warnings'].append('无UV；常量颜色可用，纹理材质需另行确认坐标')
                if not item['mesh']['materials'] or any(m is None for m in mesh.materials):
                    item['warnings'].append('存在缺失材质槽')
                if require_wind and item['mesh']['wind_range'] is None:
                    raise ValueError('缺少要求的WindMask颜色属性')
                if check_only:
                    item['status'] = 'CHECKED'
                    continue
                temp_obj = bpy.data.objects.new(stem, mesh)
                collection.objects.link(temp_obj)
                temp_obj.matrix_world = Matrix.Identity(4)
                for selected in context.selected_objects:
                    selected.select_set(False)
                temp_obj.select_set(True)
                context.view_layer.objects.active = temp_obj
                settings = dict(use_selection=True, object_types={'MESH'}, use_mesh_modifiers=False,
                                bake_anim=False, global_scale=1.0, apply_unit_scale=True,
                                apply_scale_options='FBX_SCALE_UNITS', axis_forward='-Y', axis_up='Z',
                                colors_type='LINEAR', prioritize_active_color=True, path_mode='AUTO')
                available = bpy.ops.export_scene.fbx.get_rna_type().properties
                missing = [k for k in settings if k not in available]
                if missing:
                    raise RuntimeError('当前FBX导出器缺少选项：' + ', '.join(missing))
                # TemporaryDirectory owns a new child of root, never an existing user folder.
                with tempfile.TemporaryDirectory(prefix='.fsl_export_', dir=str(root)) as staging:
                    staged = Path(staging) / (stem + '.fbx')
                    result = bpy.ops.export_scene.fbx(filepath=str(staged), **settings)
                    if 'FINISHED' not in result or not staged.is_file() or staged.stat().st_size == 0:
                        raise RuntimeError('FBX导出器没有生成非空文件')
                    item['status'] = 'EXPORTED'
                    item['fbx_bytes'] = staged.stat().st_size
                    item['fbx_settings'] = {k: sorted(v) if isinstance(v, set) else v for k,v in settings.items()}
                    staged_json = Path(staging) / (stem + '.json')
                    staged_json.write_text(json.dumps(item, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
                    _publish_new(staged, fbx, created)
                    _publish_new(staged_json, sidecar, created)
            except Exception as e:
                # Remove only files created by this item; never touch previous outputs.
                for path in reversed(created):
                    if path.parent == root and path.is_file():
                        path.unlink()
                item['status'] = 'FAILED'
                item['error'] = str(e)
            finally:
                if temp_obj:
                    bpy.data.objects.remove(temp_obj, do_unlink=True)
                if mesh and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
    finally:
        # Snapshot copies are removed by _snapshot; only our unique collection is owned here.
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(collection)
        if group:
            bpy.data.node_groups.remove(group)
        for obj in list(context.selected_objects):
            obj.select_set(False)
        for obj in original_selection:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if original_active and original_active.name in context.view_layer.objects:
            context.view_layer.objects.active = original_active
        else:
            context.view_layer.objects.active = None
        context.view_layer.update()
    report['summary'] = {state: sum(i['status'] == state for i in report['items']) for state in ['EXPORTED','CHECKED','FAILED']}
    report['report_path'] = str(report_path)
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return report


class FSLExportSettings(bpy.types.PropertyGroup):
    source_mode: EnumProperty(name='输入范围', items=[('SELECTED','所选对象','处理当前所选对象'),('COLLECTION','指定集合','处理集合及子集合中的对象')], default='SELECTED')
    source_collection: PointerProperty(name='集合', type=bpy.types.Collection)
    name_filter: StringProperty(name='名称前缀筛选', description='为空时不筛选；例如GN_Pine_只选这三棵树', default='')
    output_dir: StringProperty(name='输出目录', subtype='DIR_PATH', default='//Exports_FSL/')
    file_prefix: StringProperty(name='文件名前缀', description='将GN_替换为此前缀；其他名称补前缀一次', default='SM_')
    require_wind: BoolProperty(name='要求WindMask', description='树木导出时检查风动颜色属性；普通资产可关闭', default=False)
    last_report: StringProperty(name='上次报告', subtype='FILE_PATH')
    last_summary: StringProperty(default='选择对象或集合后，先检查再导出')


def run_operator(operator, context, check):
    s = context.scene.fsl_export_settings
    try:
        report = run_batch(context, targets(context,s), s.output_dir, s.file_prefix, s.require_wind, check)
        s.last_report = report['report_path']
        counts = report['summary']
        s.last_summary = f"{'检查' if check else '导出'}成功 {counts['CHECKED']+counts['EXPORTED']}，失败 {counts['FAILED']}"
        operator.report({'WARNING'} if counts['FAILED'] else {'INFO'}, s.last_summary)
        return {'FINISHED'}
    except Exception as e:
        s.last_summary = str(e)
        operator.report({'ERROR'}, str(e))
        return {'CANCELLED'}


class FSL_OT_Check(bpy.types.Operator):
    bl_idname = 'fsl.check_batch'
    bl_label = '检查待导出资产'
    bl_description = '检查当前求值网格并生成报告，不生成FBX'
    def execute(self, context):
        return run_operator(self,context,True)


class FSL_OT_Export(bpy.types.Operator):
    bl_idname = 'fsl.export_batch'
    bl_label = '批量导出 FBX'
    bl_description = '逐对象导出临时网格，保留源对象；同名文件停止覆盖'
    def execute(self, context):
        return run_operator(self,context,False)


class FSL_OT_Report(bpy.types.Operator):
    bl_idname = 'fsl.view_export_report'
    bl_label = '查看结果报告'
    def execute(self, context):
        path = Path(context.scene.fsl_export_settings.last_report)
        if not path.is_file():
            self.report({'ERROR'}, '请先检查或导出一次');return {'CANCELLED'}
        text = bpy.data.texts.get('FSL_Last_Batch_Report.json') or bpy.data.texts.new('FSL_Last_Batch_Report.json')
        text.clear();text.write(path.read_text(encoding='utf-8'))
        if context.area:
            context.area.type = 'TEXT_EDITOR';context.area.spaces.active.text = text
        return {'FINISHED'}


class FSL_OT_Folder(bpy.types.Operator):
    bl_idname = 'fsl.open_export_folder'
    bl_label = '打开输出文件夹'
    def execute(self, context):
        path = Path(bpy.path.abspath(context.scene.fsl_export_settings.output_dir))
        if not path.is_dir():
            self.report({'ERROR'}, '输出目录尚不存在');return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=str(path));return {'FINISHED'}


class FSL_PT_Export(bpy.types.Panel):
    bl_label = 'FSL 批量导出'
    bl_idname = 'FSL_PT_export'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'FSL'
    def draw(self, context):
        layout = self.layout
        s = context.scene.fsl_export_settings
        layout.prop(s,'source_mode')
        if s.source_mode == 'COLLECTION':
            layout.prop(s,'source_collection')
        layout.prop(s,'name_filter')
        try:
            objects = targets(context,s)
            box = layout.box();box.label(text=f'当前范围：{len(objects)} 个对象')
            for o in objects[:8]:box.label(text=o.name, icon='OUTLINER_OB_MESH' if o.type=='MESH' else 'ERROR')
            if len(objects)>8:box.label(text='其余对象见检查报告')
        except ValueError as e:
            layout.label(text=str(e),icon='INFO')
        layout.prop(s,'output_dir');layout.prop(s,'file_prefix');layout.prop(s,'require_wind')
        layout.label(text='逐件导出 · 同名阻止覆盖',icon='LOCKED')
        layout.operator('fsl.check_batch',icon='CHECKMARK')
        row=layout.row();row.scale_y=1.4;row.operator('fsl.export_batch',icon='EXPORT')
        layout.separator();layout.label(text=s.last_summary)
        row=layout.row();row.operator('fsl.view_export_report',icon='TEXT');row.operator('fsl.open_export_folder',icon='FILE_FOLDER')


CLASSES=(FSLExportSettings,FSL_OT_Check,FSL_OT_Export,FSL_OT_Report,FSL_OT_Folder,FSL_PT_Export)


def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.fsl_export_settings = PointerProperty(type=FSLExportSettings)


def unregister():
    if hasattr(bpy.types.Scene,'fsl_export_settings'):del bpy.types.Scene.fsl_export_settings
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)


if __name__ == '__main__':register()
