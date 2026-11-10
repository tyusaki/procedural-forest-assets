"""Inspect selected meshes; preserve source geometry and create a new report Text.

Run inside Blender in Object Mode. This is a teaching checker, not an exporter.
Geometry Nodes instances and collection instances are not fully expanded.
"""
import bpy
import json


def add_issue(items, severity, rule, message):
    items.append({"severity": severity, "rule": rule, "message": message})


def inspect_mesh(obj, depsgraph):
    issues = []
    has_nodes = any(mod.type == "NODES" for mod in obj.modifiers)
    result = {
        "object": obj.name,
        "mesh_data": obj.data.name,
        "source_vertices": len(obj.data.vertices),
        "object_scale": list(obj.scale),
        "modifiers": [mod.type for mod in obj.modifiers],
        "issues": issues,
    }

    # These are review rules, not automatic repair instructions.
    if not obj.name.startswith("SM_"):
        add_issue(issues, "REVIEW", "NAME_PREFIX",
                  "当前名称没有 SM_ 前缀；仅在它属于最终静态网格导出集合时确认命名。")
    if any(abs(value - 1.0) > 0.0001 for value in obj.scale):
        add_issue(issues, "REVIEW", "OBJECT_SCALE",
                  "物体自身缩放不是全 1；需结合父级、修改器和导出约定判断，不自动应用。")
    if has_nodes:
        add_issue(issues, "REVIEW", "INSTANCE_SCOPE",
                  "包含 Geometry Nodes：这里只统计求值后的直接网格，未实现实例可能未计入。")

    evaluated = obj.evaluated_get(depsgraph)
    temp_mesh = None
    try:
        temp_mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        if temp_mesh is None:
            add_issue(issues, "ERROR", "NO_MESH", "未取得可检查的网格。")
            result["status"] = "ERROR"
            return result

        temp_mesh.calc_loop_triangles()
        result["evaluated_vertices"] = len(temp_mesh.vertices)
        result["evaluated_polygons"] = len(temp_mesh.polygons)
        result["evaluated_triangles"] = len(temp_mesh.loop_triangles)
        result["uv_layers"] = [layer.name for layer in temp_mesh.uv_layers]
        result["color_attributes"] = [
            {"name": attr.name, "domain": attr.domain, "type": attr.data_type}
            for attr in temp_mesh.color_attributes
        ]
        result["material_slots"] = [
            material.name if material else None for material in temp_mesh.materials
        ]

        if not temp_mesh.polygons:
            add_issue(issues, "REVIEW" if has_nodes else "ERROR", "NO_SURFACE",
                      "直接网格没有面；若为 GN 实例输出需另查实例，不能把零当作完整场景统计。")
        if not temp_mesh.uv_layers:
            add_issue(issues, "REVIEW", "NO_UV",
                      "没有 UV 层；确认材质或导出用途是否需要 UV。存在 UV 也不代表展开质量通过。")
        if not temp_mesh.materials:
            add_issue(issues, "REVIEW", "NO_MATERIAL",
                      "没有材质槽；确认是否计划在 UE 中统一赋材质。")
        elif any(material is None for material in temp_mesh.materials):
            add_issue(issues, "REVIEW", "EMPTY_MATERIAL_SLOT", "存在空材质槽，需检查导出约定。")

        slot_count = len(temp_mesh.materials)
        if slot_count and any(face.material_index >= slot_count for face in temp_mesh.polygons):
            add_issue(issues, "ERROR", "MATERIAL_INDEX", "存在超出材质槽范围的面材质索引。")

        world_matrix = evaluated.matrix_world
        result["world_transform_determinant"] = world_matrix.to_3x3().determinant()
        if result["world_transform_determinant"] <= 0.0:
            add_issue(issues, "REVIEW", "WORLD_TRANSFORM",
                      "世界变换包含镜像或退化缩放；确认导出后的朝向、法线与形状。")

        if temp_mesh.vertices:
            minimum = [float("inf")] * 3
            maximum = [float("-inf")] * 3
            for vertex in temp_mesh.vertices:
                point = world_matrix @ vertex.co
                for axis in range(3):
                    minimum[axis] = min(minimum[axis], point[axis])
                    maximum[axis] = max(maximum[axis], point[axis])
            result["world_aabb_size_blender_units"] = [
                round(maximum[axis] - minimum[axis], 6) for axis in range(3)
            ]
    finally:
        if temp_mesh is not None:
            evaluated.to_mesh_clear()

    result["status"] = ("ERROR" if any(item["severity"] == "ERROR" for item in issues)
                        else "REVIEW" if issues else "NO_FINDINGS_IN_CHECKED_RULES")
    return result


def main():
    if bpy.context.mode != "OBJECT":
        raise RuntimeError("请先退出编辑模式，切换为 Object Mode，再运行检查。")

    selected = sorted(bpy.context.selected_objects, key=lambda obj: obj.name)
    meshes = [obj for obj in selected if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("请至少选择一个网格物体；本脚本不扫描全部场景。")

    depsgraph = bpy.context.evaluated_depsgraph_get()
    report = {
        "schema_version": 1,
        "blender_version": bpy.app.version_string,
        "source_file": bpy.data.filepath or "UNSAVED",
        "scene": bpy.context.scene.name,
        "frame": bpy.context.scene.frame_current,
        "unit_system": bpy.context.scene.unit_settings.system,
        "unit_scale_length": bpy.context.scene.unit_settings.scale_length,
        "scope": "Selected mesh objects, evaluated direct mesh, viewport depsgraph",
        "ignored_non_mesh": [obj.name for obj in selected if obj.type != "MESH"],
        "objects": [],
    }
    for obj in meshes:
        try:
            report["objects"].append(inspect_mesh(obj, depsgraph))
        except Exception as exc:
            report["objects"].append({"object": obj.name, "status": "ERROR",
                                      "exception": f"{type(exc).__name__}: {exc}"})

    # new() creates a unique name; it never replaces a previous report.
    body = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    text = bpy.data.texts.new("FSL_AssetCheck_Report.json")
    text.write(body)
    print(f"FSL inspection complete: {text.name}; objects={len(meshes)}")


if __name__ == "__main__":
    main()
