"""树木工作区：导出选中的一个成品网格，不自动修改 UE 资产。

在 Blender 的物体模式中选中且只选中一棵树，再运行此文件。
输出到当前 .blend 同目录的 Exports。已有输出时停止，不覆盖。
当前教学版本不处理修改器、骨骼、动画、实例和正式资产发布。
"""
import bpy
import json
import re
from pathlib import Path


def main(output_dir_override=None):
    # 1. 明确输入范围。
    if bpy.context.mode != "OBJECT":
        raise RuntimeError("请切换到物体模式，再运行。")
    selected = list(bpy.context.selected_objects)
    if len(selected) != 1 or selected[0].type != "MESH":
        raise RuntimeError("请只选中一个网格物体。不要同时选灯光、相机或第二棵树。")
    obj = selected[0]
    if not obj.visible_get():
        raise RuntimeError("选中的网格当前不可见，请先确认可见状态。")
    if obj.modifiers or obj.instance_type != "NONE":
        raise RuntimeError("本课只导出没有修改器、没有实例复制的普通网格；请使用 00_Trees 中的成品树。")
    if not obj.data.polygons:
        raise RuntimeError("选中的网格没有面，不能作为本课的树木导出。")
    if not bpy.data.filepath:
        raise RuntimeError("当前文件尚无保存路径，请先另存 .blend，再运行。")

    # 2. 生成路径，只改变导出文件名，不重命名 Blender 物体。
    source_path = Path(bpy.data.filepath)
    output_dir = Path(output_dir_override) if output_dir_override else source_path.parent / "Exports"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", obj.name).strip("_") or "Mesh"
    fbx_path = output_dir / (safe_name + ".fbx")
    record_path = fbx_path.with_suffix(".json")
    if fbx_path.exists() or record_path.exists():
        raise RuntimeError(f"已有本次练习文件，停止以免覆盖。请先查看：{output_dir}")

    # 3. 把导出条件集中在一起，便于记录和复现。
    settings = {
        "use_selection": True,
        "object_types": {"MESH"},
        "use_mesh_modifiers": False,
        "bake_anim": False,
        "global_scale": 1.0,
        "apply_unit_scale": True,
        "apply_scale_options": "FBX_SCALE_UNITS",
        "axis_forward": "-Y",
        "axis_up": "Z",
        "colors_type": "LINEAR",
        "prioritize_active_color": True,
    }
    # 在创建文件之前，确认本机导出器认识这些选项。
    available = bpy.ops.export_scene.fbx.get_rna_type().properties
    missing = [name for name in settings if name not in available]
    if missing:
        raise RuntimeError("本机 FBX 导出器缺少选项：" + ", ".join(missing))

    warnings = []
    if not obj.data.uv_layers:
        warnings.append("没有 UV：本次只练习基础网格交接，不代表贴图材质验收通过。")
    if not obj.data.color_attributes.get("WindMask"):
        warnings.append("没有 WindMask：不能用本次输出替换已经完成风动的正式树木。")
    active_color = obj.data.color_attributes.active_color
    if obj.data.color_attributes.get("WindMask") and (not active_color or active_color.name != "WindMask"):
        warnings.append("WindMask 不是当前活动颜色属性；需另行验证 UE 实际收到的顶点颜色。")
    if bpy.context.scene.unit_settings.system == "NONE":
        warnings.append("场景未明确使用物理单位；导入尺寸需要实际核对。")

    record = {
        "schema_version": 1,
        "status": "FBX_WRITTEN_UE_UNVERIFIED",
        "blender_version": bpy.app.version_string,
        "source_file": str(source_path),
        "source_has_unsaved_changes": bpy.data.is_dirty,
        "scene": bpy.context.scene.name,
        "frame": bpy.context.scene.frame_current,
        "object": obj.name,
        "object_scale": list(obj.scale),
        "unit_system": bpy.context.scene.unit_settings.system,
        "unit_scale_length": bpy.context.scene.unit_settings.scale_length,
        "dimensions_blender_units": list(obj.dimensions),
        "color_attributes": [a.name for a in obj.data.color_attributes],
        "active_color_attribute": active_color.name if active_color else None,
        "fbx_file": str(fbx_path),
        "settings": {k: sorted(v) if isinstance(v, set) else v for k, v in settings.items()},
        "warnings": warnings,
    }

    # 4. 调用导出器，再检查磁盘上确实产生了非空文件。
    output_dir.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.export_scene.fbx(filepath=str(fbx_path), **settings)
    if "FINISHED" not in result or not fbx_path.is_file() or fbx_path.stat().st_size == 0:
        raise RuntimeError("未确认导出成功；请检查输出目录和 Blender 的错误信息。")
    record["fbx_bytes"] = fbx_path.stat().st_size
    body = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
    # x 表示只能创建新文件；若路径已存在就报错。
    with record_path.open("x", encoding="utf-8") as stream:
        stream.write(body)
    report = bpy.data.texts.get("FSL_Export_Result.json") or bpy.data.texts.new("FSL_Export_Result.json")
    report.clear()
    report.write(body)
    # 直接显示结果，减少在文本下拉菜单中找文件的困扰。
    if bpy.context.area and bpy.context.area.type == "TEXT_EDITOR":
        bpy.context.space_data.text = report
    print(f"导出练习完成：{fbx_path}；交接记录：{record_path}")
    return record


if __name__ == "__main__":
    main()
