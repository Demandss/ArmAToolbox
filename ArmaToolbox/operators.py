import bpy
import bpy_extras
from lists import safeAddTime
from . import properties
from ArmaProxy import CopyProxy, CreateProxyPos, SelectProxy
import bmesh
import ArmaTools
import RVMatTools
from math import *
from mathutils import *
from ArmaToolbox import getLodsToFix
from traceback import print_tb

from properties import ArmaToolboxMaterialProperties
# from RtmTools import exportModelCfg

import os, tempfile, fnmatch, re
import errno, sys
from MDLImporter import importMDL

# Sadly, Python fails to provide the following magic number for us.
ERROR_INVALID_NAME = 123

'''
Windows-specific error code indicating an invalid pathname.

See Also
----------
https://docs.microsoft.com/en-us/windows/win32/debug/system-error-codes--0-499-
    Official listing of all such codes.
'''


def is_pathname_valid(pathname: str) -> bool:
    '''
    `True` if the passed pathname is a valid pathname for the current OS;
    `False` otherwise.
    '''
    # If this pathname is either not a string or is but is empty, this pathname
    # is invalid.
    try:
        if not isinstance(pathname, str) or not pathname:
            return False

        # Strip this pathname's Windows-specific drive specifier (e.g., `C:\`)
        # if any. Since Windows prohibits path components from containing `:`
        # characters, failing to strip this `:`-suffixed prefix would
        # erroneously invalidate all valid absolute Windows pathnames.
        _, pathname = os.path.splitdrive(pathname)

        # Directory guaranteed to exist. If the current OS is Windows, this is
        # the drive to which Windows was installed (e.g., the "%HOMEDRIVE%"
        # environment variable); else, the typical root directory.
        root_dirname = os.environ.get('HOMEDRIVE', 'C:') \
            if sys.platform == 'win32' else os.path.sep
        assert os.path.isdir(root_dirname)  # ...Murphy and her ironclad Law

        # Append a path separator to this directory if needed.
        root_dirname = root_dirname.rstrip(os.path.sep) + os.path.sep

        # Test whether each path component split from this pathname is valid or
        # not, ignoring non-existent and non-readable path components.
        for pathname_part in pathname.split(os.path.sep):
            try:
                os.lstat(root_dirname + pathname_part)
            # If an OS-specific exception is raised, its error code
            # indicates whether this pathname is valid or not. Unless this
            # is the case, this exception implies an ignorable kernel or
            # filesystem complaint (e.g., path not found or inaccessible).
            #
            # Only the following exceptions indicate invalid pathnames:
            #
            # * Instances of the Windows-specific "WindowsError" class
            #   defining the "winerror" attribute whose value is
            #   "ERROR_INVALID_NAME". Under Windows, "winerror" is more
            #   fine-grained and hence useful than the generic "errno"
            #   attribute. When a too-long pathname is passed, for example,
            #   "errno" is "ENOENT" (i.e., no such file or directory) rather
            #   than "ENAMETOOLONG" (i.e., file name too long).
            # * Instances of the cross-platform "OSError" class defining the
            #   generic "errno" attribute whose value is either:
            #   * Under most POSIX-compatible OSes, "ENAMETOOLONG".
            #   * Under some edge-case OSes (e.g., SunOS, *BSD), "ERANGE".
            except OSError as exc:
                if hasattr(exc, 'winerror'):
                    if exc.winerror == ERROR_INVALID_NAME:
                        return False
                elif exc.errno in {errno.ENAMETOOLONG, errno.ERANGE}:
                    return False
    # If a "TypeError" exception was raised, it almost certainly has the
    # error message "embedded NUL character" indicating an invalid pathname.
    except TypeError as exc:
        return False
    # If no exception was raised, all path components and hence this
    # pathname itself are valid. (Praise be to the curmudgeonly python.)
    else:
        return True
    # If any other exception was raised, this is an unrelated fatal issue
    # (e.g., a bug). Permit this exception to unwind the call stack.
    #
    # Did we mention this should be shipped with Python already?


def is_path_creatable(pathname: str) -> bool:
    '''
    `True` if the current user has sufficient permissions to create the passed
    pathname; `False` otherwise.
    '''
    # Parent directory of the passed path. If empty, we substitute the current
    # working directory (CWD) instead.
    dirname = os.path.dirname(pathname) or os.getcwd()
    return os.access(dirname, os.W_OK)


def is_path_exists_or_creatable(pathname: str) -> bool:
    '''
    `True` if the passed pathname is a valid pathname for the current OS _and_
    either currently exists or is hypothetically creatable; `False` otherwise.

    This function is guaranteed to _never_ raise exceptions.
    '''
    try:
        # To prevent "os" module calls from raising undesirable exceptions on
        # invalid pathnames, is_pathname_valid() is explicitly called first.
        return is_pathname_valid(pathname) and (
                os.path.exists(pathname) or is_path_creatable(pathname))
    # Report failure on non-fatal filesystem complaints (e.g., connection
    # timeouts, permissions issues) implying this path to be inaccessible. All
    # other exceptions are unrelated fatal issues and should not be caught here.
    except OSError:
        return False


def is_path_sibling_creatable(pathname: str) -> bool:
    '''
    `True` if the current user has sufficient permissions to create **siblings**
    (i.e., arbitrary files in the parent directory) of the passed pathname;
    `False` otherwise.
    '''
    # Parent directory of the passed path. If empty, we substitute the current
    # working directory (CWD) instead.
    dirname = os.path.dirname(pathname) or os.getcwd()

    try:
        # For safety, explicitly close and hence delete this temporary file
        # immediately after creating it in the passed path's parent directory.
        with tempfile.TemporaryFile(dir=dirname):
            pass
        return True
    # While the exact type of exception raised by the above function depends on
    # the current version of the Python interpreter, all such types subclass the
    # following exception superclass.
    except EnvironmentError:
        return False


def is_path_exists_or_creatable_portable(pathname: str) -> bool:
    '''
    `True` if the passed pathname is a valid pathname on the current OS _and_
    either currently exists or is hypothetically creatable in a cross-platform
    manner optimized for POSIX-unfriendly filesystems; `False` otherwise.

    This function is guaranteed to _never_ raise exceptions.
    '''
    try:
        # To prevent "os" module calls from raising undesirable exceptions on
        # invalid pathnames, is_pathname_valid() is explicitly called first.
        return is_pathname_valid(pathname) and (
                os.path.exists(pathname) or is_path_sibling_creatable(pathname))
    # Report failure on non-fatal filesystem complaints (e.g., connection
    # timeouts, permissions issues) implying this path to be inaccessible. All
    # other exceptions are unrelated fatal issues and should not be caught here.
    except OSError:
        return False


class ATBX_OT_add_frame_range(bpy.types.Operator):
    bl_idname = "armatoolbox.add_frame_range"
    bl_label = ""
    bl_description = "Add a range of keyframes"

    def execute(self, context):
        obj = context.active_object
        prp = obj.armaObjProps.keyFrames
        guiProps = context.window_manager.armaGUIProps
        start = guiProps.framePanelStart
        end = guiProps.framePanelEnd
        step = guiProps.framePanelStep

        for frame in range(start, end, step):
            safeAddTime(frame, prp)

        return {"FINISHED"}


class ATBX_OT_add_key_frame(bpy.types.Operator):
    bl_idname = "armatoolbox.add_key_frame"
    bl_label = ""
    bl_description = "Add a keyframe"

    def execute(self, context):
        obj = context.active_object
        prp = obj.armaObjProps.keyFrames
        frame = context.scene.frame_current
        safeAddTime(frame, prp)
        return {"FINISHED"}


class ATBX_OT_add_all_key_frames(bpy.types.Operator):
    bl_idname = "armatoolbox.add_all_key_frames"
    bl_label = ""
    bl_description = "Add a keyframe"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj.animation_data is not None and obj.animation_data.action is not None

    def execute(self, context):
        obj = context.active_object
        prp = obj.armaObjProps.keyFrames

        keyframes = []

        if obj.animation_data is not None and obj.animation_data.action is not None:
            fcurves = obj.animation_data.action.fcurves
            for curve in fcurves:
                for kp in curve.keyframe_points:
                    if kp.co.x not in keyframes:
                        keyframes.append(kp.co.x)

        keyframes.sort()

        for frame in keyframes:
            safeAddTime(frame, prp)

        return {"FINISHED"}


class ATBX_OT_rem_key_frame(bpy.types.Operator):
    bl_idname = "armatoolbox.rem_key_frame"
    bl_label = ""
    bl_description = "Remove a keyframe"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        arma = obj.armaObjProps
        return arma.keyFramesIndex != -1

    def execute(self, context):
        obj = context.active_object
        arma = obj.armaObjProps
        if arma.keyFramesIndex != -1:
            arma.keyFrames.remove(arma.keyFramesIndex)
        return {"FINISHED"}


class ATBX_OT_rem_all_key_frames(bpy.types.Operator):
    bl_idname = "armatoolbox.rem_all_key_frames"
    bl_label = ""
    bl_description = "Remove a keyframe"

    def execute(self, context):
        obj = context.active_object
        arma = obj.armaObjProps
        arma.keyFrames.clear()
        return {"FINISHED"}


class ATBX_OT_add_prop(bpy.types.Operator):
    bl_idname = "armatoolbox.add_prop"
    bl_label = ""
    bl_description = "Add a named Property"

    def execute(self, context):
        obj = context.active_object
        prp = obj.armaObjProps.namedProps
        item = prp.add()
        item.name = "<new property>"
        return {"FINISHED"}


class ATBX_OT_rem_prop(bpy.types.Operator):
    bl_idname = "armatoolbox.rem_prop"
    bl_label = ""
    bl_description = "Remove named property"

    def execute(self, context):
        obj = context.active_object
        arma = obj.armaObjProps
        if arma.namedPropIndex != -1:
            arma.namedProps.remove(arma.namedPropIndex)
        return {"FINISHED"}


###
##   Enable Operator
#

class ATBX_OT_enable(bpy.types.Operator):
    bl_idname = "armatoolbox.enable"
    bl_label = "Enable for Arma Toolbox"

    def execute(self, context):
        obj = context.active_object
        if (obj.armaObjProps.isArmaObject == False):
            obj.armaObjProps.isArmaObject = True
        return {'FINISHED'}


###
##   Proxy Operators
#


class ATBX_OT_add_new_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.add_new_proxy"
    bl_label = ""
    bl_description = "Add a proxy"

    def execute(self, context):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action='DESELECT')
        obj = context.active_object
        mesh = obj.data

        cursor_location = bpy.context.scene.cursor.location
        cursor_location = cursor_location - obj.location

        bm = bmesh.from_edit_mesh(mesh)

        i = len(bm.verts)
        v1 = bm.verts.new(cursor_location + Vector((0, 0, 0)))
        v2 = bm.verts.new(cursor_location + Vector((0, 0, 1)))
        v3 = bm.verts.new(cursor_location + Vector((0, 0.5, 0)))

        f = bm.faces.new((v1, v2, v3))
        print(v1.index)
        bpy.ops.object.mode_set(mode="OBJECT")

        # bm.to_mesh(mesh)
        # mesh.update()

        vgrp = obj.vertex_groups.new(name="@@armaproxy")
        vgrp.add([i + 0], 1, 'ADD')
        vgrp.add([i + 1], 1, 'ADD')
        vgrp.add([i + 2], 1, 'ADD')

        fnd = obj.vertex_groups.find("-all-proxies")
        if (fnd == -1):
            vgrp2 = obj.vertex_groups.new(name="-all-proxies")
        else:
            vgrp2 = obj.vertex_groups[fnd]

        vgrp2.add([i + 0], 1, 'ADD')
        vgrp2.add([i + 1], 1, 'ADD')
        vgrp2.add([i + 2], 1, 'ADD')

        bpy.ops.object.mode_set(mode="EDIT")

        p = obj.armaObjProps.proxyArray.add()
        p.name = vgrp.name

        return {"FINISHED"}

    # This does two things. First, it goes through the model checking if the


# proxies in the list are still in the model, and delete all that arent.
#
# Secondly, it looks for "standard" proxy definition like "proxy:xxxx.001" and
# converts them into new proxies.
class ATBX_OT_add_sync_proxies(bpy.types.Operator):
    bl_idname = "armatoolbox.sync_proxies"
    bl_label = ""
    bl_description = "Synchronize the proxy list with the model"

    def execute(self, context):
        obj = context.active_object
        prp = obj.armaObjProps.proxyArray

        # Gp through our proxy list
        delList = []
        max = prp.values().__len__()
        for i in range(0, max):
            if prp[i].name not in obj.vertex_groups.keys():
                delList.append(i)
        if len(delList) > 0:
            delList.reverse()
            for item in delList:
                prp.remove(item)

        # Go through the vertex groups
        for grp in obj.vertex_groups:
            if len(grp.name) > 5:
                if grp.name[:6] == "proxy:":
                    prx = grp.name.split(":")[1]
                    if prx.find(".") != -1:
                        a = prx.split(".")
                        prx = a[0]
                        idx = a[1]
                    else:
                        idx = "1"
                    n = prp.add()
                    n.name = grp.name
                    n.index = int(idx)
                    n.path = "P:" + prx

        return {"FINISHED"}


class ATBX_OT_add_toggle_proxies(bpy.types.Operator):
    bl_idname = "armatoolbox.toggle_proxies"
    bl_label = ""
    bl_description = "Toggle GUI visibilityl"

    prop: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.active_object
        prop = obj.armaObjProps.proxyArray[self.prop]
        if prop.open == True:
            prop.open = False
        else:
            prop.open = True
        return {"FINISHED"}


class ATBX_OT_copy_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.copy_proxy"
    bl_label = ""
    bl_description = "Copy proxy to other LOD's"

    objectArray: bpy.props.CollectionProperty(type=properties.ArmaToolboxCopyHelper)
    copyProxyName: bpy.props.StringProperty()
    encloseInto: bpy.props.StringProperty(
        description="Enclose the proxy in a selection. Leave blank to not create any extra selection")

    def execute(self, context):
        sObj = context.active_object
        for obj in self.objectArray:
            if obj.doCopy:
                enclose = self.encloseInto
                enclose = enclose.strip()
                if len(enclose) == 0:
                    enclose = None

                CopyProxy(sObj, bpy.data.objects[obj.name], self.copyProxyName, enclose)

        self.objectArray.clear()
        return {"FINISHED"}

    def invoke(self, context, event):

        for obj in bpy.data.objects.values():
            if obj.armaObjProps.isArmaObject == True and obj != context.active_object:
                prop = self.objectArray.add()
                prop.name = obj.name
                prop.doCopy = True

        wm = context.window_manager
        return wm.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Copy Proxy!")
        for s in self.objectArray:
            row = layout.row()
            row.prop(s, "doCopy", text=s.name)
        row = layout.row()
        row.prop(self, "encloseInto", text="Enclose in:")


class ATBX_OT_delete_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.delete_proxy"
    bl_label = ""
    bl_description = "Delete given proxy"

    proxyName: bpy.props.StringProperty()

    @classmethod
    def poll(self, context):
        if context.active_object.mode == 'OBJECT':
            return True
        else:
            return False

    def execute(self, context):
        sObj = context.active_object
        mesh = sObj.data

        idxList = []

        grp = sObj.vertex_groups[self.proxyName]
        for v in mesh.vertices:
            for g in v.groups:
                if g.group == grp.index:
                    if g.weight > 0:
                        idxList.append(v.index)

        if len(idxList) > 0:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            if hasattr(bm.verts, "ensure_lookup_table"):
                bm.verts.ensure_lookup_table()

            vList = []
            for i in idxList:
                vList.append(bm.verts[i])

            for v in vList:
                bm.verts.remove(v)

            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

        sObj.vertex_groups.remove(grp)
        prp = sObj.armaObjProps.proxyArray
        for i, pa in enumerate(prp):
            if pa.name == self.proxyName:
                prp.remove(i)

        return {"FINISHED"}


class ATBX_OT_select_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.select_proxy"
    bl_label = ""
    bl_description = "selects given proxy"

    proxyName: bpy.props.StringProperty()

    @classmethod
    def poll(self, context):
        if context.active_object.mode == 'EDIT':
            return True
        else:
            return False

    def execute(self, context):
        sObj = context.active_object
        SelectProxy(sObj, self.proxyName)
        return {"FINISHED"}


class ATBX_OT_separating_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.separating_proxy"
    bl_label = ""
    bl_description = "Separating the proxy from the mesh"

    proxyName: bpy.props.StringProperty()

    @classmethod
    def poll(self, context):
        if context.active_object.mode == 'EDIT':
            return True
        else:
            return False

    def execute(self, context):
        sObj = context.active_object
        SelectProxy(sObj, self.proxyName)
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode="OBJECT")

        sObj = context.selected_objects[len(context.selected_objects)-1]
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = sObj
        sObj.name = self.proxyName
        sObj.select_set(True)

        coll = None
        try:
            coll = bpy.data.collections["Proxy"]
        except Exception as e:
            coll = bpy.data.collections.new("Proxy")
            active_coll = context.view_layer.active_layer_collection.collection
            active_coll.children.link(coll)

        for ob in sObj.users_collection[:]:
            ob.objects.unlink(sObj)

        coll.objects.link(sObj)

        return {"FINISHED"}

###
##  Weight Tools
#

class ATBX_OT_select_weight_vertex(bpy.types.Operator):
    bl_idname = "armatoolbox.sel_weights"
    bl_label = "Select vertices with more than 4 bones weights"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        ArmaTools.selectOverweightVertices()
        return {'FINISHED'}


class ATBX_OT_prune_weight_vertex(bpy.types.Operator):
    bl_idname = "armatoolbox.prune_weights"
    bl_label = "Prune vertices with more than 4 bones weights"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        ArmaTools.pruneOverweightVertices()
        return {'FINISHED'}


class ATBX_OT_bulk_rename(bpy.types.Operator):
    bl_idname = "armatoolbox.bulk_rename"
    bl_label = "Bulk Rename Arma material paths"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        if guiProps.renamableListIndex > -1 and guiProps.renamableList.__len__() > guiProps.renamableListIndex:
            frm = guiProps.renamableList[guiProps.renamableListIndex].name
            t = guiProps.renameTo
            ArmaTools.bulkRename(context, frm, t)
        return {'FINISHED'}


class ATBX_OT_create_components(bpy.types.Operator):
    bl_idname = "armatoolbox.create_components"
    bl_label = "Create Geometry Components"

    def execute(self, context):
        ArmaTools.createComponents(context)
        return {'FINISHED'}


class ATBX_OT_bulk_reparent(bpy.types.Operator):
    bl_idname = "armatoolbox.bulk_reparent"
    bl_label = "Bulk Re-parent Arma material paths"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        frm = guiProps.parentFrom
        t = guiProps.parentTo
        ArmaTools.bulkReparent(context, frm, t)
        return {'FINISHED'}


class ATBX_OT_selection_rename(bpy.types.Operator):
    bl_idname = "armatoolbox.bulk_rename_selection"
    bl_label = "Bulk Re-parent Arma material paths"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        frm = guiProps.renameSelectionFrom
        t = guiProps.renameSelectionTo
        ArmaTools.bulkRenameSelections(context, frm, t)
        return {'FINISHED'}


class ATBX_OT_hitpoint_creator(bpy.types.Operator):
    bl_idname = "armatoolbox.hitpoint_creator"
    bl_label = "Create Hitpoint Volume"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        selection = guiProps.hpCreatorSelectionName
        radius = guiProps.hpCreatorRadius
        ArmaTools.hitpointCreator(context, selection, radius)
        return {'FINISHED'}


# Make sure all ArmaObjects do not have n-gons with more than four vertices
class ATBX_OT_ensure_quads(bpy.types.Operator):
    '''Make sure all ArmaObjects do not have n-gons with more than four vertices'''
    bl_idname = "armatoolbox.ensure_quads"
    bl_label = "Tesselate all n-gons > 4 in all objects flagged as Arma Objects"

    def execute(self, context):
        ArmaTools.tessNonQuads(context)
        return {'FINISHED'}


class ATBX_OT_rvmat_relocator(bpy.types.Operator):
    '''Relocate a single RVMat to a different directory'''
    bl_idname = "armatoolbox.rvmatrelocator"
    bl_label = "Relocate RVMat"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        rvfile = guiProps.rvmatRelocFile
        rvout = guiProps.rvmatOutputFolder
        prefixPath = guiProps.matPrefixFolder
        RVMatTools.rt_CopyRVMat(rvfile, rvout, prefixPath)
        return {'FINISHED'}


class ATBX_OT_material_relocator(bpy.types.Operator):
    ''' Relocate RVMATs'''
    bl_idname = "armatoolbox.materialrelocator"
    bl_label = "Relocate Material"

    material: bpy.props.StringProperty()
    texture: bpy.props.StringProperty()

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps

        outputPath = guiProps.matOutputFolder
        if len(outputPath) is 0:
            self.report({'ERROR_INVALID_INPUT'}, "Output folder name missing")

        prefixPath = guiProps.matPrefixFolder
        if len(prefixPath) == 0:
            prefixPath = "P:\\"

        materialName = self.material
        textureName = self.texture
        RVMatTools.mt_RelocateMaterial(textureName, materialName, outputPath, guiProps.matAutoHandleRV, prefixPath)

        return {'FINISHED'}


class ATBX_OT_toggle_gui_prop(bpy.types.Operator):
    bl_idname = "armatoolbox.toggleguiprop"
    bl_label = ""
    bl_description = "Toggle GUI visibilityl"

    prop: bpy.props.StringProperty()

    def execute(self, context):
        prop = context.window_manager.armaGUIProps
        if prop.is_property_set(self.prop) == True and prop[self.prop] == True:
            prop[self.prop] = False
        else:
            prop[self.prop] = True
        return {"FINISHED"}


class ATBX_OT_join_as_proxy(bpy.types.Operator):
    bl_idname = "armatoolbox.joinasproxy"
    bl_label = ""
    bl_description = "Join as Proxy"

    @classmethod
    def poll(cls, context):
        ## Visible when there is a selected object, it is a mesh
        obj = context.active_object

        return (obj
                and obj.select_get() == True
                and obj.armaObjProps.isArmaObject == True
                and obj.type == "MESH"
                and len(bpy.context.selected_objects) > 1
                )

    def execute(self, context):
        obj = context.active_object
        selected = context.selected_objects

        path = context.window_manager.armaGUIProps.mapProxyObject
        index = context.window_manager.armaGUIProps.mapProxyIndex
        doDel = context.window_manager.armaGUIProps.mapProxyDelete

        enclose = context.window_manager.armaGUIProps.mapProxyEnclose
        if len(enclose) == 0:
            enclose = None

        for sel in selected:
            if sel == obj:
                pass
            else:
                if enclose == None:
                    e = None
                else:
                    e = enclose + str(index)
                pos = sel.location - obj.location
                CreateProxyPos(obj, pos, path, index, e)
                index = index + 1

        if doDel == True:
            obj.select_set(False)
            bpy.ops.object.delete();

        return {"FINISHED"}

    ###


##  Proxy Path Changer
#
#   Code contributed by Cowcancry

class ATBX_OT_proxy_path_changer(bpy.types.Operator):
    bl_idname = "armatoolbox.proxypathchanger"
    bl_label = "Proxy Path Change"

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        pathFrom = guiProps.proxyPathFrom
        pathTo = guiProps.proxyPathTo

        for obj in bpy.data.objects.values():
            if (obj
                    and obj.armaObjProps.isArmaObject == True
                    and obj.type == "MESH"):
                for proxy in obj.armaObjProps.proxyArray:
                    proxy.path = proxy.path.replace(pathFrom, pathTo)
        return {'FINISHED'}


class ATBX_OT_selection_translator(bpy.types.Operator):
    bl_idname = "armatoolbox.autotranslate"
    bl_label = "Attempt to automatically translate Czech selection names"

    def execute(self, context):
        ArmaTools.autotranslateSelections()
        return {'FINISHED'}


class ATBX_OT_set_mass(bpy.types.Operator):
    bl_idname = "armatoolbox.setmass"
    bl_label = ""
    bl_description = "Set the same mass for all selected vertices"

    @classmethod
    def poll(cls, context):
        ## Visible when there is a selected object, it is a mesh
        obj = context.active_object
        return (obj
                and obj.select_get() == True
                and obj.armaObjProps.isArmaObject == True
                and obj.type == "MESH"
                and (obj.armaObjProps.lod == '1.000e+13' or obj.armaObjProps.lod == '4.000e+13')
                and obj.mode == 'EDIT'
                )

    def execute(self, context):
        obj = context.active_object
        selected = context.selected_objects

        mass = context.window_manager.armaGUIProps.vertexWeight

        ArmaTools.setVertexMass(obj, mass)
        return {"FINISHED"}


class ATBX_OT_distribute_mass(bpy.types.Operator):
    bl_idname = "armatoolbox.distmass"
    bl_label = ""
    bl_description = "Distribute the given mass equally to all selected vertices"

    @classmethod
    def poll(cls, context):
        ## Visible when there is a selected object, it is a mesh
        obj = context.active_object
        return (obj
                and obj.select_get() == True
                and obj.armaObjProps.isArmaObject == True
                and obj.type == "MESH"
                and (obj.armaObjProps.lod == '1.000e+13' or obj.armaObjProps.lod == '4.000e+13')
                and obj.mode == 'EDIT'
                )

    def execute(self, context):
        obj = context.active_object
        selected = context.selected_objects

        mass = context.window_manager.armaGUIProps.vertexWeight

        ArmaTools.distributeVertexMass(obj, mass)
        return {"FINISHED"}

    # Fix shadow volumes


class ATBX_OT_fix_shadows(bpy.types.Operator):
    bl_idname = "armatoolbox.fixshadows"
    bl_label = "Items to fix"
    bl_description = "Fix Shadow volumes resolutions"

    objectArray: bpy.props.CollectionProperty(type=properties.ArmaToolboxFixShadowsHelper)

    def execute(self, context):
        for prop in self.objectArray:
            obj = bpy.data.objects[prop.name]

            lod = obj.armaObjProps.lod
            res = obj.armaObjProps.lodDistance

            print(obj.name, " ", lod, " ", res)

            if lod == "1.000e+4":
                if res == 0:
                    obj.armaObjProps.lodDistance = 1.0
            elif lod == "1.001e+4":
                print("Stencil 2")
                obj.armaObjProps.lod = "1.000e+4"
                obj.armaObjProps.lodDistance = 10.0
            elif lod == "1.100e+4":
                if res == 0:
                    obj.armaObjProps.lodDistance = 1.0
            elif lod == "1.101e+4":
                obj.armaObjProps.lod = "1.100e+4"
                obj.armaObjProps.lodDistance = 10.0

        self.objectArray.clear()
        return {"FINISHED"}

    def invoke(self, context, event):
        self.objectArray.clear()
        objs = getLodsToFix()
        for o in objs:
            prop = self.objectArray.add()
            prop.name = o.name
            prop.fixThis = True

        wm = context.window_manager
        return wm.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.label(icon="MODIFIER", text="The following objects have shadow volume issues")
        col = layout.column()

        for s in self.objectArray:
            row = layout.row()
            row.prop(s, "fixThis", text=s.name)

        row = layout.row()
        row.label(text="Click 'OK' to fix")


""" class ATBX_OT_export_bone(bpy.types.Operator):
    bl_idname = "armatoolbox.exportbone"
    bl_label = ""
    bl_description = "Export a bone as model.cfg animation"

    @classmethod
    def poll(cls, context):
        obj = context.active_object

        return (obj
            and obj.select_get() == True
            and obj.armaObjProps.isArmaObject == True
            and obj.type == "ARMATURE"
            )  

    def execute(self, context):
        obj = context.active_object
        arma = obj.armaObjProps

        exportModelCfg(context, obj, arma.exportBone, arma.selectionName, arma.animSource, arma.prefixString, arma.outputFile)

        return {"FINISHED"}  """


class ATBX_OT_section_optimize(bpy.types.Operator):
    bl_idname = "armatoolbox.section_optimize"
    bl_label = "Optimize section count."
    bl_description = "Available in Edit mode. Select transparent faces and click this button to minimize section count."

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        ArmaTools.optimizeSectionCount(context)

        return {"FINISHED"}


class ATBX_OT_vgroup_redefine(bpy.types.Operator):
    bl_idname = "armatoolbox.vgroup_redefine"
    bl_label = "Redefine Vertex Group"
    bl_description = "Delete the vertex group and recreate it with the selected vertices only"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        obj = context.active_object
        try:
            vg_name = obj.vertex_groups.active.name
            bpy.ops.object.vertex_group_remove()
            vgrp = bpy.context.active_object.vertex_groups.new(name=vg_name)
            bpy.ops.object.vertex_group_assign()
        except:
            pass
        return {"FINISHED"}


class ATBX_OT_join(bpy.types.Operator):
    bl_idname = "armatoolbox.join"
    bl_label = "Join selected objects with active object"
    bl_description = "Joins all selected objects with the active one, maintaining the proxies of all joined objects"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        ArmaTools.joinObjectToObject(context)
        return {"FINISHED"}


class ATBX_OT_selectBadUV(bpy.types.Operator):
    bl_idname = "armatoolbox.select_bad_uv"
    bl_label = "Select distorted UV islands"
    bl_description = "select all islands with UV distortion."

    def execute(self, context):
        guiProps = context.window_manager.armaGUIProps
        naxAngle = radians(guiProps.uvIslandAngle)

        ArmaTools.selectBadUV(self, context, naxAngle)

        return {'FINISHED'}


class ATBX_OT_set_transparency(bpy.types.Operator):
    bl_idname = "armatoolbox.set_transparency"
    bl_label = "Mark as Transparent"
    bl_description = "Mark selected faces as transparent for sorting purposes"

    @classmethod
    def poll(self, context):
        if context.active_object != None and context.active_object.mode == 'EDIT':
            return True
        else:
            return False

    def execute(self, context):
        ArmaTools.markTransparency(self, context, 1)
        return {'FINISHED'}


class ATBX_OT_unset_transparency(bpy.types.Operator):
    bl_idname = "armatoolbox.unset_transparency"
    bl_label = "Mark as non-transparent"
    bl_description = "Mark selected faces as non-transparent for sorting purposes"

    @classmethod
    def poll(self, context):
        if context.active_object != None and context.active_object.mode == 'EDIT':
            return True
        else:
            return False

    def execute(self, context):
        ArmaTools.markTransparency(self, context, 0)
        return {'FINISHED'}


class ATBX_OT_select_transparent(bpy.types.Operator):
    bl_idname = "armatoolbox.select_transparent"
    bl_label = "Select transparent faces"
    bl_description = "Select all faces marked as transparent"

    @classmethod
    def poll(self, context):
        if context.active_object != None and context.active_object.mode == 'EDIT':
            return True
        else:
            return False

    def execute(self, context):
        ArmaTools.selectTransparency(self, context)
        return {'FINISHED'}


class ATBX_OT_process_materials(bpy.types.Operator):
    bl_idname = "armatoolbox.process_materials"
    bl_label = "Process Materials"

    def execute(self, context):
        from bpy.types import Object, Collection, Material, NodeTree, ShaderNodeTexImage, ShaderNodeBsdfPrincipled, \
            Image

        from subprocess import call

        import random
        import string

        import winreg

        # Computer\HKEY_CURRENT_USER\Software\Bohemia Interactive\Dayz Tools\ImageToPAA
        regKey = r"Software\Bohemia Interactive\Dayz Tools\ImageToPAA"
        reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER, regKey)  # winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)

        images = {}
        imageToPaa = winreg.QueryValueEx(reg, 'tool')[0]
        tempFolder = '%temp%\\armatoolbox\\'
        tempFolder = os.path.expandvars(tempFolder)

        for _col in bpy.data.collections:
            col: Collection = _col
            objects = [obj
                       for obj in col.all_objects
                       if obj.type == 'MESH'
                       and obj.armaObjProps.isArmaObject
                       ]
            for _obj in objects:
                obj: Object = _obj
                for _material in obj.material_slots:
                    try:
                        material: Material = _material.material
                        material.use_nodes = True

                        props: ArmaToolboxMaterialProperties = material.armaMatProps

                        node_tree: NodeTree = material.node_tree

                        texture_path = "P:\\" + props.texture
                        texture_path = texture_path.lower()

                        if is_path_exists_or_creatable_portable(texture_path) == False:
                            continue

                        try:
                            tempName = images[texture_path]
                        except KeyError:
                            file_name = os.path.basename(texture_path)
                            file_name = os.path.splitext(file_name)[0]

                            tempName = tempFolder + col.name + "_" + file_name + ".png"

                            command = '"' + imageToPaa + '" "' + texture_path + '" "' + tempName + '"'
                            call(command, shell=True)

                            images[texture_path] = tempName

                        image = bpy.data.images.load(tempName)
                        image.source = 'FILE'
                        image.name = tempName

                        tex_node: ShaderNodeTexImage = node_tree.nodes.get('Texture')
                        if tex_node is None:
                            tex_node = node_tree.nodes.new(type="ShaderNodeTexImage")

                        tex_node.image = image
                        tex_node.name = 'Texture'

                        principled_node: ShaderNodeBsdfPrincipled = node_tree.nodes.get('Principled BSDF')

                        links = node_tree.links

                        link = links.new
                        link(tex_node.outputs[0], principled_node.inputs[0])
                    except Exception as e:
                        self.report({'WARNING', 'INFO'}, "I/O error: {0}".format(e))

        return {'FINISHED'}


class ATBX_OT_import_proxy_mlod(bpy.types.Operator):
    bl_idname = "armatoolbox.import_proxy_mlod"
    bl_label = ""
    bl_description = "Import mlod for proxies"

    proxyName: bpy.props.StringProperty()

    @classmethod
    def poll(self, context):
        if context.active_object.mode == 'OBJECT':
            return True
        else:
            return False

    def execute(self, context):
        global file_path
        mlods_path = context.window_manager.armaGUIProps.mlodDayZFolder
        mlod_suffix = context.window_manager.armaGUIProps.mlodSuffix
        mlodEmptyProxy = context.window_manager.armaGUIProps.mlodEmptyProxy
        mlodEmptyProxyFile = context.window_manager.armaGUIProps.mlodEmptyProxyFile
        obj = context.active_object

        if mlodEmptyProxy == True and mlodEmptyProxyFile != "":
            file_path = mlodEmptyProxyFile
        else:
            for prox in obj.armaObjProps.proxyArray:
                file_path = prox.path

        error = -2
        try:
            error = importMDL(context, file_path, False, 1, True)
        except Exception as e:
            exc_tb = sys.exc_info()[2]
            print_tb(exc_tb)
            print("{0}".format(exc_tb))
            self.report({'WARNING', 'INFO'}, "I/O error: {0}\n{1}".format(e, exc_tb))

        obj_name = file_path[file_path.rfind("\\") + 1:].split(".")[0] + '_1'

        if error == -1 or error == -2:
            for root, dirs, files, in os.walk(mlods_path):
                for name in files:
                    if (name.casefold()).__eq__((obj_name[:obj_name.rfind("_")] + mlod_suffix + ".p3d").casefold()):
                        obj_name = obj_name[:obj_name.rfind("_")] + mlod_suffix + '_1'
                        error = importMDL(context, os.path.join(root, name), False, 1, True)
                    elif (name.casefold()).__eq__((obj_name[:obj_name.rfind("_")] + ".p3d").casefold()):
                        obj_name = obj_name[:obj_name.rfind("_")] + '_1'
                        error = importMDL(context, os.path.join(root, name), False, 1, True)

        if error == -1:
            self.report({'WARNING', 'INFO'}, "I/O error: Wrong MDL version")
        if error == -2:
            self.report({'WARNING', 'INFO'}, "I/O error: Exception while reading")

        obj.select_set(True)

        for f_obj in bpy.context.scene.objects:
            if obj_name[:obj_name.rfind("_")] in f_obj.name:
                f_obj.select_set(True)

        try:
            bpy.ops.object.join()
        except Exception as e:
            self.report({'WARNING', 'INFO'}, "I/O error: Looks like the visual model could not be loaded.")

        bpy.data.collections.remove(bpy.data.collections[obj_name[:obj_name.rfind("_")]])

        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode="OBJECT")

        obj = context.selected_objects[len(context.selected_objects) - 1]

        obj_name = context.active_object.name

        context.view_layer.objects.active = obj
        obj.name = obj_name + "_mlod"
        obj.select_set(True)

        coll = None
        try:
            coll = bpy.data.collections["Mlod"]
        except Exception as e:
            coll = bpy.data.collections.new("Mlod")
            active_coll = context.view_layer.active_layer_collection.collection
            active_coll.children.link(coll)

        for ob in obj.users_collection[:]:
            ob.objects.unlink(obj)

        coll.objects.link(obj)

        bpy.ops.object.parent_set(type="OBJECT", keep_transform=False)

        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)

        obj.armaObjProps.isArmaObject = False

        return {"FINISHED"}


op_classes = (
    ATBX_OT_add_frame_range,
    ATBX_OT_add_key_frame,
    ATBX_OT_add_all_key_frames,
    ATBX_OT_rem_key_frame,
    ATBX_OT_rem_all_key_frames,
    ATBX_OT_add_prop,
    ATBX_OT_rem_prop,
    ATBX_OT_enable,
    ATBX_OT_add_new_proxy,
    ATBX_OT_add_sync_proxies,
    ATBX_OT_add_toggle_proxies,
    ATBX_OT_copy_proxy,
    ATBX_OT_delete_proxy,
    ATBX_OT_select_weight_vertex,
    ATBX_OT_prune_weight_vertex,
    ATBX_OT_bulk_rename,
    ATBX_OT_create_components,
    ATBX_OT_bulk_reparent,
    ATBX_OT_selection_rename,
    ATBX_OT_hitpoint_creator,
    ATBX_OT_ensure_quads,
    ATBX_OT_rvmat_relocator,
    ATBX_OT_material_relocator,
    ATBX_OT_toggle_gui_prop,
    ATBX_OT_join_as_proxy,
    ATBX_OT_proxy_path_changer,
    ATBX_OT_selection_translator,
    ATBX_OT_set_mass,
    ATBX_OT_distribute_mass,
    ATBX_OT_fix_shadows,
    # ATBX_OT_export_bone,
    ATBX_OT_section_optimize,
    ATBX_OT_vgroup_redefine,
    ATBX_OT_select_proxy,
    ATBX_OT_separating_proxy,
    ATBX_OT_join,
    ATBX_OT_selectBadUV,
    ATBX_OT_set_transparency,
    ATBX_OT_unset_transparency,
    ATBX_OT_select_transparent,
    ATBX_OT_process_materials,
    ATBX_OT_import_proxy_mlod
)


def register():
    from bpy.utils import register_class
    for c in op_classes:
        register_class(c)


def unregister():
    from bpy.utils import unregister_class
    for c in op_classes:
        unregister_class(c)