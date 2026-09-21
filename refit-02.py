#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refit.py
=============================================================================
Riconversione "morbida" di mesh (STL, o STEP nato da mesh) in B-Rep analitica.

Il principio e' uno solo: OGNI modifica alla geometria e' locale, viene
verificata subito (solido ancora chiuso, facce valide, orientamento coerente,
area e volume coerenti con la mesh) e se non passa il controllo viene
SCARTATA. Quello che non si riesce a convertire resta tassellato com'era: il
file di uscita e' sempre coerente con quello di ingresso, al massimo e' meno
"pulito". Non si cuce (Sewing) e non si ricostruisce niente globalmente.

FASE A (-a) : unione delle facce complanari e degli spigoli collineari.
              Due facce si fondono solo se TUTTI i vertici del gruppo unito
              stanno entro la tolleranza lineare dal piano comune.
FASE B (-b) : FORI CIRCOLARI. Pareti cilindriche chiuse a 360 gradi, concave,
              che sboccano su due facce piane ortogonali all'asse, con i due
              bordi che sono cerchi esatti -> un cilindro analitico con due
              cerchi. Niente altro: e' la fase prudente.
FASE C (-c) : raccordi, smussi, svasature, lamature, sfere d'angolo, bossi:
              ogni regione di faccette che sta su un cilindro / cono / sfera /
              toro viene sostituita dalla superficie analitica, una regione
              alla volta, con lo stesso controllo e lo stesso scarto. Quello
              che la Fase B rifiuta (fori lamati, fori che sboccano su una
              faccia curva) qui passa, con i bordi lasciati poligonali dove
              non c'e' una curva esatta.

Senza -a/-b/-c girano tutte e tre le fasi in sequenza. -b e -c eseguono
sempre anche la Fase A prima, e una Fase A finale dopo: le facce piane
spezzate dalle sostituzioni si riuniscono (con la tolleranza data a -a).

COME LEGGERE IL REPORT (--report)
---------------------------------
Una riga per regione, col tipo di primitiva, la copertura angolare, il numero
di faccette, lo scarto vertici-superficie (rms e max) e l'esito:

  [N spigoli analitici . N riusati . N poligonali]
      analitici = bordi rifatti con la curva esatta (cerchio, ellisse,
                  intersezione delle due superfici);
      riusati   = bordi gia' buoni, presi dalla mesh senza toccarli;
      poligonali= bordi lasciati come spezzata della mesh perche' nessuna
                  curva esatta li descrive entro tolleranza.
  "bordi coi piani lasciati poligonali"  = secondo tentativo: la faccia e'
      analitica ma i bordi coi piani vicini restano quelli della mesh.
  "contorno lasciato poligonale"         = terzo tentativo: nessuno spigolo
      nuovo, il contorno resta identico alla mesh (serve quando il vicino e'
      una faccia analitica gia' chiusa, col suo seam, che non va toccata).
  "scartata: ..." spiega il motivo: la regione resta tassellata. In
      particolare "regione troppo piccola in mezzo a faccette tassellate"
      vuol dire che era una primitiva fittata sul rumore (raggi a caso in
      zona di raccordo): la si lascia alla mesh, si rifinisce a mano.

VELOCITA' (-j N)
----------------
La ricerca delle primitive dai semi, che e' la fetta piu' grossa delle Fasi B
e C, gira su piu' PROCESSI: -j N ne usa fino a N (default: i core della
macchina meno due, al massimo 22). Non thread, perche' sia OpenCascade sia il
codice numpy tengono il GIL e sui thread non si guadagna niente. Il numero di
processi viene poi proporzionato al lavoro da fare: su un pezzo piccolo
accenderne venti costa piu' di quanto faccia risparmiare. Restano su un core
solo la Fase A e le sostituzioni nel solido: lavorano su una struttura
OpenCascade condivisa e vanno verificate una alla volta.
Con piu' processi i semi vengono provati su una fotografia delle faccette gia'
prese, quindi l'insieme delle regioni trovate puo' cambiare di poco rispetto a
-j 1; ogni regione resta comunque verificata e scartata con gli stessi
criteri. Per un risultato riproducibile al 100% si usa -j 1.

USO
---
    python refit.py pezzo.stl                 # A + B + C
    python refit.py pezzo.stl -a              # solo Fase A
    python refit.py pezzo.stl -a 0.01         # solo Fase A, unione a 10 micron
    python refit.py pezzo.stl -b              # A + fori
    python refit.py pezzo.stl -c              # A + raccordi/lavorazioni
    python refit.py pezzo.stl -b -c --report  # tutto, con report .txt
    python refit.py pezzo.stl -b -c -a 0.01   # tutto, riunione finale a 10 um
    python refit.py pezzo.stl -b -c -i 2      # due cicli B/C prima di salvare
    python refit.py pezzo.stl -j 22           # fino a 22 processi di lavoro
    python refit.py pezzo.stl -j 1            # tutto in sequenza, riproducibile
    python refit.py pezzo.step -o out.step    # ingresso STEP
    python refit.py --check                   # verifica l'ambiente

DIPENDENZE
----------
    pip install cadquery-ocp numpy       (OCP, consigliato su Windows)
    oppure conda install -c conda-forge pythonocc-core numpy
"""

from __future__ import annotations

import argparse
import importlib
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# =============================================================================
# 0. LOGGER
# =============================================================================


class _C:
    RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
    GREY = "\033[90m"; RED = "\033[91m"; GREEN = "\033[92m"
    YELLOW = "\033[93m"; BLUE = "\033[94m"; MAGENTA = "\033[95m"; CYAN = "\033[96m"


def _enable_vt_windows() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for handle_id in (-11, -12):
            h = k.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


_LEVELS = {"DEBUG": 10, "INFO": 20, "OK": 25, "WARN": 30, "ERROR": 40}
_STYLE = {
    "DEBUG": (_C.GREY, "···"), "INFO": (_C.CYAN, " i "), "OK": (_C.GREEN, " ✓ "),
    "WARN": (_C.YELLOW, " ! "), "ERROR": (_C.RED, " ✗ "),
}


def _out(text: str) -> None:
    """print() che non muore sulle console senza UTF-8 (cp1252, pipe)."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)


class Log:
    level = 20
    no_color = False
    _sink: List[str] = []

    @classmethod
    def _emit(cls, lvl: str, msg: str) -> None:
        cls._sink.append(f"[{lvl:<5}] {msg}")
        if _LEVELS[lvl] < cls.level:
            return
        col, tag = _STYLE[lvl]
        if cls.no_color:
            _out(f"[{tag.strip()}] {msg}")
        else:
            _out(f"{col}{tag}{_C.RESET} {msg}")

    @classmethod
    def debug(cls, m): cls._emit("DEBUG", m)
    @classmethod
    def info(cls, m): cls._emit("INFO", m)
    @classmethod
    def ok(cls, m): cls._emit("OK", m)
    @classmethod
    def warn(cls, m): cls._emit("WARN", m)
    @classmethod
    def error(cls, m): cls._emit("ERROR", m)

    @classmethod
    def banner(cls, title: str) -> None:
        line = "─" * max(10, 74 - len(title))
        if cls.no_color:
            _out(f"\n== {title} {line}")
        else:
            _out(f"\n{_C.BOLD}{_C.MAGENTA}══ {title} {line}{_C.RESET}")
        cls._sink.append(f"\n=== {title} ===")

    @classmethod
    def dump(cls, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(cls._sink))


# =============================================================================
# 1. IMPORT OpenCascade (OCP di cadquery oppure OCC.Core di pythonocc)
# =============================================================================


def _load_occ():
    for ns in ("OCP", "OCC.Core"):
        try:
            importlib.import_module(ns + ".gp")
            return ns
        except ImportError:
            continue
    return None


_NS = _load_occ()
if _NS is None:
    print("\n[X] OpenCascade non trovato.\n"
          "    pip install cadquery-ocp   (oppure conda install -c conda-forge pythonocc-core)\n",
          file=sys.stderr)
    sys.exit(2)


def _m(name: str):
    return importlib.import_module(f"{_NS}.{name}")


def _st(cls, name: str):
    """
    Metodo STATICO di una classe OCC, qualunque sia il binding:
      OCP        : BRep_Tool.Pnt_s
      pythonocc  : BRep_Tool.Pnt   (oppure brep_tool.Pnt / BRep_Tool_Pnt)
    """
    for nm in (name + "_s", name):
        f = getattr(cls, nm, None)
        if callable(f):
            return f
    mod = sys.modules.get(cls.__module__)
    for nm in (f"{cls.__name__}_{name}",):
        f = getattr(mod, nm, None) if mod else None
        if callable(f):
            return f
    low = getattr(mod, cls.__name__.lower(), None) if mod else None
    f = getattr(low, name, None) if low is not None else None
    if callable(f):
        return f
    raise AttributeError(f"{cls.__name__}.{name} non disponibile in {_NS}")


_gp = _m("gp"); _Geom = _m("Geom"); _Geom2d = _m("Geom2d")
_TopAbs = _m("TopAbs"); _TopoDS = _m("TopoDS"); _TopExp = _m("TopExp")
_TopTools = _m("TopTools"); _BRep = _m("BRep"); _BRepAdaptor = _m("BRepAdaptor")
_BRepBuilderAPI = _m("BRepBuilderAPI"); _BRepGProp = _m("BRepGProp")
_GProp = _m("GProp"); _GeomAbs = _m("GeomAbs"); _ShapeUpgrade = _m("ShapeUpgrade")
_ShapeFix = _m("ShapeFix"); _BRepCheck = _m("BRepCheck"); _STEPControl = _m("STEPControl")
_IFSelect = _m("IFSelect"); _Interface = _m("Interface"); _BRepTools = _m("BRepTools")
_ShapeAnalysis = _m("ShapeAnalysis"); _BRepClass3d = _m("BRepClass3d")
_TColgp = _m("TColgp"); _TColStd = _m("TColStd"); _Geom2dAPI = _m("Geom2dAPI")
_TopLoc = _m("TopLoc")

gp_Pnt, gp_Dir, gp_Vec, gp_Ax2, gp_Ax3, gp_Pnt2d = (
    _gp.gp_Pnt, _gp.gp_Dir, _gp.gp_Vec, _gp.gp_Ax2, _gp.gp_Ax3, _gp.gp_Pnt2d)
TopAbs_FACE = _TopAbs.TopAbs_FACE; TopAbs_WIRE = _TopAbs.TopAbs_WIRE
TopAbs_EDGE = _TopAbs.TopAbs_EDGE; TopAbs_VERTEX = _TopAbs.TopAbs_VERTEX
TopAbs_SOLID = _TopAbs.TopAbs_SOLID; TopAbs_SHELL = _TopAbs.TopAbs_SHELL
TopAbs_REVERSED = _TopAbs.TopAbs_REVERSED; TopAbs_FORWARD = _TopAbs.TopAbs_FORWARD
TopAbs_OUT = _TopAbs.TopAbs_OUT; TopAbs_IN = _TopAbs.TopAbs_IN
TopExp_Explorer = _TopExp.TopExp_Explorer
TopTools_IndexedMapOfShape = _TopTools.TopTools_IndexedMapOfShape
TopTools_IndexedDataMapOfShapeListOfShape = _TopTools.TopTools_IndexedDataMapOfShapeListOfShape
BRep_Tool = _BRep.BRep_Tool; BRep_Builder = _BRep.BRep_Builder
BRepAdaptor_Surface = _BRepAdaptor.BRepAdaptor_Surface
BRepAdaptor_Curve = _BRepAdaptor.BRepAdaptor_Curve
GeomAbs_Plane = _GeomAbs.GeomAbs_Plane; GeomAbs_Cylinder = _GeomAbs.GeomAbs_Cylinder
GeomAbs_Cone = _GeomAbs.GeomAbs_Cone; GeomAbs_Sphere = _GeomAbs.GeomAbs_Sphere
GeomAbs_Torus = _GeomAbs.GeomAbs_Torus
GeomAbs_Line = _GeomAbs.GeomAbs_Line; GeomAbs_Circle = _GeomAbs.GeomAbs_Circle
GeomAbs_Ellipse = _GeomAbs.GeomAbs_Ellipse
GProp_GProps = _GProp.GProp_GProps
BRepCheck_Analyzer = _BRepCheck.BRepCheck_Analyzer
TopoDS_Compound = _TopoDS.TopoDS_Compound; TopoDS_Shell = _TopoDS.TopoDS_Shell
TopoDS_Face = _TopoDS.TopoDS_Face; TopoDS_Wire = _TopoDS.TopoDS_Wire
TopoDS_Edge = _TopoDS.TopoDS_Edge; TopoDS_Vertex = _TopoDS.TopoDS_Vertex
ShapeUpgrade_UnifySameDomain = _ShapeUpgrade.ShapeUpgrade_UnifySameDomain
ShapeFix_Shape = _ShapeFix.ShapeFix_Shape; ShapeFix_Solid = _ShapeFix.ShapeFix_Solid
STEPControl_Reader = _STEPControl.STEPControl_Reader
STEPControl_Writer = _STEPControl.STEPControl_Writer
STEPControl_AsIs = _STEPControl.STEPControl_AsIs
IFSelect_RetDone = _IFSelect.IFSelect_RetDone
BRepBuilderAPI_MakeEdge = _BRepBuilderAPI.BRepBuilderAPI_MakeEdge
BRepBuilderAPI_MakeWire = _BRepBuilderAPI.BRepBuilderAPI_MakeWire
BRepBuilderAPI_MakeFace = _BRepBuilderAPI.BRepBuilderAPI_MakeFace
BRepBuilderAPI_MakeSolid = _BRepBuilderAPI.BRepBuilderAPI_MakeSolid
BRepTools_ReShape = _BRepTools.BRepTools_ReShape
BRepClass3d_SolidClassifier = _BRepClass3d.BRepClass3d_SolidClassifier
ShapeAnalysis_ShapeTolerance = _ShapeAnalysis.ShapeAnalysis_ShapeTolerance
Geom_Plane = _Geom.Geom_Plane; Geom_CylindricalSurface = _Geom.Geom_CylindricalSurface
Geom_ConicalSurface = _Geom.Geom_ConicalSurface
Geom_SphericalSurface = _Geom.Geom_SphericalSurface
Geom_ToroidalSurface = _Geom.Geom_ToroidalSurface
Geom_Line = _Geom.Geom_Line; Geom_Circle = _Geom.Geom_Circle; Geom_Ellipse = _Geom.Geom_Ellipse
Geom2d_BSplineCurve = _Geom2d.Geom2d_BSplineCurve
TopLoc_Location = _TopLoc.TopLoc_Location


# --- cast TopoDS_Shape -> sotto-tipo ---------------------------------------
def _cast(kind: str):
    holder = getattr(_TopoDS, "topods", None)
    if holder is not None and hasattr(holder, kind):
        return getattr(holder, kind)
    fn = getattr(_TopoDS, "topods_" + kind, None)
    if fn is not None:
        return fn
    cls = getattr(_TopoDS, "TopoDS", None)
    if cls is not None:
        for nm in (kind + "_s", kind):
            f = getattr(cls, nm, None)
            if callable(f):
                return f
    raise AttributeError(f"cast TopoDS -> {kind} non disponibile in {_NS}")


td_Face, td_Edge, td_Vertex = _cast("Face"), _cast("Edge"), _cast("Vertex")
td_Shell, td_Wire, td_Solid = _cast("Shell"), _cast("Wire"), _cast("Solid")

# --- metodi statici usati ovunque -------------------------------------------
bt_Pnt = _st(BRep_Tool, "Pnt")
bt_Tolerance = _st(BRep_Tool, "Tolerance")
bt_Range = _st(BRep_Tool, "Range")
bt_Curve = _st(BRep_Tool, "Curve")
bt_Surface = _st(BRep_Tool, "Surface")
bt_Degenerated = _st(BRep_Tool, "Degenerated")
te_FirstVertex = _st(_TopExp.TopExp, "FirstVertex")
te_LastVertex = _st(_TopExp.TopExp, "LastVertex")
te_MapShapes = _st(_TopExp.TopExp, "MapShapes")
te_MapAncestors = _st(_TopExp.TopExp, "MapShapesAndAncestors")
gp_Surface = _st(_BRepGProp.BRepGProp, "SurfaceProperties")
gp_Volume = _st(_BRepGProp.BRepGProp, "VolumeProperties")
gp_Linear = _st(_BRepGProp.BRepGProp, "LinearProperties")
brt_OuterWire = _st(_BRepTools.BRepTools, "OuterWire")


def _iface_set(key: str, val: str) -> None:
    IS = _Interface.Interface_Static
    for fn in ("SetCVal_s", "SetCVal"):
        if hasattr(IS, fn):
            try:
                getattr(IS, fn)(key, val)
                return
            except Exception:
                pass


# =============================================================================
# 2. UTILITY TOPOLOGICHE
# =============================================================================


def _size(m) -> int:
    for attr in ("Extent", "Size"):
        fn = getattr(m, attr, None)
        if callable(fn):
            return int(fn())
    return int(len(m))


def _iter_list(lst):
    """
    Itera una TopTools_ListOfShape. ⚠️ Con OCP l'iterazione nativa (for s in
    lst) e' 50 volte piu' veloce del ListIterator chiamato da Python: su un
    pezzo da 10.000 spigoli fa la differenza fra 3 s e 0.1 s per indice.
    """
    try:
        return list(lst)
    except TypeError:
        pass
    it_cls = getattr(_TopTools, "TopTools_ListIteratorOfListOfShape", None)
    out = []
    if it_cls is not None:
        it = it_cls(lst)
        while it.More():
            out.append(it.Value())
            it.Next()
    return out


def explore(shape, kind) -> List:
    """Sotto-shape unici (senza duplicati, orientazione ignorata)."""
    m = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, kind, m)
    return [m.FindKey(i) for i in range(1, _size(m) + 1)]


def count_sub(shape, kind) -> int:
    m = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, kind, m)
    return _size(m)


def shape_stats(shape) -> Dict[str, int]:
    return {"solids": count_sub(shape, TopAbs_SOLID),
            "shells": count_sub(shape, TopAbs_SHELL),
            "faces": count_sub(shape, TopAbs_FACE),
            "edges": count_sub(shape, TopAbs_EDGE),
            "verts": count_sub(shape, TopAbs_VERTEX)}


def edge_face_map(shape):
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    te_MapAncestors(shape, TopAbs_EDGE, TopAbs_FACE, m)
    return m


def count_free_edges(shape) -> int:
    """Spigoli con una sola faccia: 0 = guscio chiuso."""
    m = edge_face_map(shape)
    return sum(1 for i in range(1, _size(m) + 1)
               if _size(m.FindFromIndex(i)) == 1)


_SFT = _ShapeFix.ShapeFix_ShapeTolerance


def set_tolerance(sub, tol: float) -> None:
    """
    IMPOSTA la tolleranza di un vertice/spigolo (anche piu' bassa).
    ⚠️ BRep_Builder.UpdateVertex/UpdateEdge(tol) possono solo ALZARLA: un
    rollback fatto con quelle non riporta mai indietro niente.
    """
    try:
        _SFT().SetTolerance(sub, float(max(tol, 1e-9)), sub.ShapeType())
    except Exception:
        pass


def face_edges_map(shape):
    """spigolo(indice mappa) -> [facce] costruita per traversata delle facce.
    ⚠️ ef.FindFromIndex(k) copia una lista C++ a ogni chiamata (0.3 ms): su
    10.000 spigoli x 30 ricostruzioni sono 80 s. Cosi' e' quasi gratis."""
    emap = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, TopAbs_EDGE, emap)
    fmap = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, TopAbs_FACE, fmap)
    e_faces = [[] for _ in range(_size(emap))]
    for i in range(1, _size(fmap) + 1):
        f = fmap.FindKey(i)
        ex = TopExp_Explorer(f, TopAbs_EDGE)
        seen = set()
        while ex.More():
            k = emap.FindIndex(ex.Current())
            if k > 0 and k not in seen:
                seen.add(k)
                e_faces[k - 1].append(i - 1)
            ex.Next()
    return emap, fmap, e_faces


def vpos(v) -> np.ndarray:
    p = bt_Pnt(td_Vertex(v))
    return np.array([p.X(), p.Y(), p.Z()], dtype=float)


def face_vertices(face) -> np.ndarray:
    return np.array([vpos(v) for v in explore(face, TopAbs_VERTEX)], dtype=float)


def face_area(face) -> float:
    g = GProp_GProps()
    gp_Surface(face, g)
    return abs(float(g.Mass()))


def shape_volume(shape) -> float:
    g = GProp_GProps()
    gp_Volume(shape, g)
    return float(g.Mass())


def edge_length(edge) -> float:
    g = GProp_GProps()
    gp_Linear(edge, g)
    return float(g.Mass())


def face_surface_type(face) -> int:
    return BRepAdaptor_Surface(td_Face(face), True).GetType()


def face_plane_normal(face) -> Optional[np.ndarray]:
    """Normale USCENTE di una faccia planare (None se non planare)."""
    f = td_Face(face)
    ad = BRepAdaptor_Surface(f, True)
    if ad.GetType() != GeomAbs_Plane:
        return None
    d = ad.Plane().Axis().Direction()
    n = np.array([d.X(), d.Y(), d.Z()], dtype=float)
    if f.Orientation() == TopAbs_REVERSED:
        n = -n
    nn = np.linalg.norm(n)
    return n / nn if nn > 1e-12 else None


def face_plane_point(face) -> np.ndarray:
    ad = BRepAdaptor_Surface(td_Face(face), True)
    p = ad.Plane().Location()
    return np.array([p.X(), p.Y(), p.Z()], dtype=float)


def max_tolerance(shape) -> float:
    try:
        return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1))
    except Exception:
        return float("nan")


_BC_NAMES: Dict[int, str] = {}
for _n in dir(_BRepCheck):
    if _n.startswith("BRepCheck_") and _n != "BRepCheck_Analyzer":
        try:
            _BC_NAMES[int(getattr(_BRepCheck, _n))] = _n[10:]
        except Exception:
            pass


def check_detail(shape, limit: int = 6) -> List[str]:
    """Errori di BRepCheck in chiaro ([] = valida)."""
    try:
        an = BRepCheck_Analyzer(shape)
        if an.IsValid():
            return []
    except Exception as e:
        return [f"BRepCheck: {e}"]
    out: List[str] = []
    for kind, lab in ((TopAbs_FACE, "faccia"), (TopAbs_WIRE, "wire"),
                      (TopAbs_EDGE, "edge"), (TopAbs_VERTEX, "vertice")):
        for s in explore(shape, kind):
            try:
                res = an.Result(s)
            except Exception:
                continue
            if res is None:
                continue
            try:
                sts = list(res.Status())
            except Exception:
                sts = []
            for st in sts:
                nm = str(st).split("_")[-1]
                if nm and nm != "NoError":
                    tag = f"{lab}:{nm}"
                    if tag not in out:
                        out.append(tag)
                    if len(out) >= limit:
                        return out
    return out


def is_valid(shape) -> bool:
    try:
        return bool(BRepCheck_Analyzer(shape).IsValid())
    except Exception:
        return False


def make_solid_from_faces(shape):
    """Shell (chiuso, si spera) + solido, con verso controllato."""
    b = BRep_Builder()
    shell = TopoDS_Shell()
    b.MakeShell(shell)
    for f in explore(shape, TopAbs_FACE):
        b.Add(shell, f)
    ms = BRepBuilderAPI_MakeSolid()
    ms.Add(shell)
    sol = ms.Solid()
    try:
        cl = BRepClass3d_SolidClassifier(sol)
        cl.PerformInfinitePoint(1e-6)
        if cl.State() == TopAbs_IN:
            Log.warn("Normali della mesh rivolte verso l'interno: inverto il guscio.")
            sol = td_Solid(sol.Reversed())
    except Exception:
        pass
    return sol


def ensure_solid(shape):
    """Se la shape non e' un solido (compound/shell), prova a farne uno."""
    if count_sub(shape, TopAbs_SOLID) >= 1:
        return shape
    return make_solid_from_faces(shape)


# =============================================================================
# 3. I/O
# =============================================================================


def read_stl(path: str):
    """STL binario o ASCII -> solido di facce triangolari con spigoli CONDIVISI."""
    t0 = time.perf_counter()
    RWStl = _m("RWStl").RWStl
    tri = _st(RWStl, "ReadFile")(path)
    if tri is None or tri.NbTriangles() == 0:
        Log.error(f"STL vuoto o illeggibile: {path}")
        sys.exit(3)
    mk = _BRepBuilderAPI.BRepBuilderAPI_MakeShapeOnMesh(tri)
    mk.Build()
    sh = mk.Shape()
    sol = make_solid_from_faces(sh)
    st = shape_stats(sol)
    Log.ok(f"STL letto in {time.perf_counter()-t0:.2f}s -> {os.path.basename(path)}  "
           f"({tri.NbTriangles():,} triangoli · {tri.NbNodes():,} vertici)")
    fe = count_free_edges(sol)
    if fe:
        Log.warn(f"Mesh non chiusa: {fe:,} spigoli con una sola faccia. "
                 f"Restano aperti anche in uscita (non si inventa geometria).")
    return sol, st


def read_step(path: str):
    t0 = time.perf_counter()
    r = STEPControl_Reader()
    if r.ReadFile(path) != IFSelect_RetDone:
        Log.error(f"Lettura STEP fallita: {path}")
        sys.exit(3)
    r.TransferRoots()
    shape = r.OneShape()
    if shape.IsNull():
        Log.error("Lo STEP non contiene geometria trasferibile.")
        sys.exit(3)
    shape = ensure_solid(shape)
    st = shape_stats(shape)
    Log.ok(f"STEP letto in {time.perf_counter()-t0:.2f}s -> {os.path.basename(path)}  "
           f"({st['faces']:,} facce · {st['solids']} solidi)")
    return shape, st


def read_input(path: str):
    if not os.path.isfile(path):
        Log.error(f"File non trovato: {path}")
        sys.exit(2)
    if os.path.splitext(path)[1].lower() == ".stl":
        return read_stl(path)
    return read_step(path)


def write_step(shape, path: str, schema: str = "AP214IS") -> None:
    _iface_set("write.step.schema", schema)
    _iface_set("write.step.unit", "MM")
    _iface_set("write.precision.mode", "0")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    if w.Write(path) != IFSelect_RetDone:
        Log.error(f"Scrittura STEP fallita: {path}")
        return
    Log.ok(f"Salvato: {path}  ({os.path.getsize(path)/1024:,.0f} KB)")


# =============================================================================
# 4. FASE A — UNIONE FACCE COMPLANARI
# =============================================================================
#
# ⚠️ PERCHE' NON BASTA UnifySameDomain CON UNA TOLLERANZA ANGOLARE.
# Il criterio "normali entro X gradi" non ha una taratura giusta:
#   - con 0.05 gradi (il vecchio default) sul pezzo di prova le strisce lunghe
#     38 mm dei raccordi venivano fuse coi triangoli delle sfere d'angolo
#     (0.01-0.05 gradi di differenza) e la faccia fusa si portava dietro una
#     tolleranza di 0.03 mm: geometria deformata, in silenzio;
#   - con 0.005 gradi sul secondo pezzo (triangoli da 0.001 mm2, rumore float32
#     dell'STL di 0.01-0.03 gradi) non si fonde piu' quasi niente.
# Il criterio giusto e' una DISTANZA: due faccette stanno sullo stesso piano se
# TUTTI i vertici del gruppo stanno entro lin_tol dal piano medio del gruppo.
# Le faccette piccole e rumorose si fondono (la deviazione e' microscopica),
# le strisce lunghe e storte no. I gruppi si calcolano qui in numpy e si
# passano a UnifySameDomain bloccando (KeepShape) gli spigoli fra gruppi
# diversi: cosi' OCC costruisce le facce fuse, ma fonde solo cio' che diciamo.


def face_arrays(shape):
    """
    Dati per il clustering planare di QUALSIASI shape a facce planari (anche
    dopo le fasi B/C: le facce curve restano gruppi a se').
    Ritorna V, liste di indici vertice per faccia, normali (None se curva),
    aree, mappa spigoli, facce per spigolo.
    """
    emap, fmap, e_faces = face_edges_map(shape)
    vmap = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, TopAbs_VERTEX, vmap)
    V = np.array([vpos(vmap.FindKey(j)) for j in range(1, _size(vmap) + 1)], dtype=float).reshape(-1, 3)
    fv, nrm, areas = [], [], []
    for i in range(1, _size(fmap) + 1):
        f = td_Face(fmap.FindKey(i))
        m = TopTools_IndexedMapOfShape()
        te_MapShapes(f, TopAbs_VERTEX, m)
        fv.append([vmap.FindIndex(m.FindKey(k)) - 1 for k in range(1, _size(m) + 1)])
        nrm.append(face_plane_normal(f))
        areas.append(face_area(f))
    return V, fv, nrm, np.array(areas), emap, fmap, e_faces


# ⚠️ ang_max 6 gradi: con -a 0.05 su un raccordo r=4 la sola distanza fonderebbe
# faccette fino a 18 gradi l'una dall'altra e il raccordo diventerebbe un
# poligono a spigoli vivi. Le facce davvero complanari differiscono di
# frazioni di grado, quindi il tetto angolare non toglie niente di vero.
def planar_clusters(V, fv, nrm, areas, e_faces, lin_tol: float, ang_max_deg: float = 6.0,
                    narrow_frac: float = 0.5):
    """
    Etichetta di gruppo planare per ogni faccia (union-find).
    Due criteri, entrambi obbligatori:
      1. locale : sin(angolo fra le normali) x estensione massima <= 2 lin_tol,
                  cioe' la deviazione REALE che l'angolo produce sulla faccia
                  grande. Una faccetta minuscola e rumorosa passa, una
                  striscia lunga 38 mm inclinata di 0.03 gradi no;
      2. globale: tutti i vertici del gruppo unito entro lin_tol dal piano medio.
    Con lin_tol = 0.01 si uniscono anche le facce di una superficie bombata di
    10 micron: e' la scelta dell'utente, e la tolleranza risultante viene
    ricalcolata e dichiarata.
    """
    nF = len(fv)
    planar = [n is not None for n in nrm]
    N = np.array([n if n is not None else np.zeros(3) for n in nrm])
    cent = np.array([V[idx].mean(axis=0) if idx else np.zeros(3) for idx in fv])
    Lmax = np.array([float(np.linalg.norm(V[idx].max(axis=0) - V[idx].min(axis=0))) if idx else 0.0
                     for idx in fv])
    # faccia "stretta": area molto minore del quadrato della sua estensione.
    # La sua normale vale poco, il criterio locale non si applica.
    narrow = np.array([a < 0.05 * L * L for a, L in zip(areas, np.maximum(Lmax, 1e-9))])
    narrow_tol = narrow_frac * lin_tol
    pairs = [(fs[0], fs[1]) for fs in e_faces if len(fs) == 2 and planar[fs[0]] and planar[fs[1]]]
    parent = np.arange(nF)
    csum_n = N * areas[:, None]
    csum_c = cent * areas[:, None]
    carea = areas.copy()
    cverts = [set(idx) for idx in fv]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if pairs:
        pa = np.array(pairs)
        cosd = np.abs(np.einsum("ij,ij->i", N[pa[:, 0]], N[pa[:, 1]]))
        ang = np.degrees(np.arccos(np.clip(cosd, -1.0, 1.0)))
        sin_a = np.sqrt(np.maximum(0.0, 1.0 - cosd ** 2))
        dev_loc = sin_a * np.maximum(Lmax[pa[:, 0]], Lmax[pa[:, 1]])
        # coppie ammesse SOLO perche' una delle due facce e' una scheggia:
        # vanno verificate col piano ai minimi quadrati, non con la media.
        slim = (dev_loc > 2.0 * lin_tol)
        for k in np.argsort(ang, kind="stable"):
            if ang[k] > ang_max_deg:
                break
            if dev_loc[k] > 2.0 * lin_tol and not (narrow[pa[k, 0]] or narrow[pa[k, 1]]):
                continue
            a_, c_ = find(pa[k, 0]), find(pa[k, 1])
            if a_ == c_:
                continue
            ns = csum_n[a_] + csum_n[c_]
            nn = np.linalg.norm(ns)
            if nn < 1e-12:
                continue
            nrm_ = ns / nn
            cen = (csum_c[a_] + csum_c[c_]) / (carea[a_] + carea[c_])
            vs = np.fromiter(cverts[a_] | cverts[c_], dtype=np.int64)
            Q = V[vs]
            # ⚠️ la normale MEDIA non e' il piano migliore: una scheggia lunga
            # e stretta (0.3 x 47 mm) ha la normale mal determinata, e basta
            # un grado per buttare via un'unione che ai minimi quadrati
            # sarebbe entro il micron. Per quelle schegge si usa il piano ai
            # minimi quadrati di TUTTI i vertici dell'unione, ma con una
            # tolleranza molto piu' stretta: assorbire una scheggia non deve
            # costare precisione al resto del pezzo.
            if float(np.abs((Q - cen) @ nrm_).max()) > lin_tol or slim[k]:
                if min(carea[a_], carea[c_]) > 0.2 * max(carea[a_], carea[c_]):
                    continue
                cen2 = Q.mean(axis=0)
                try:
                    _, _, Vt = np.linalg.svd(Q - cen2, full_matrices=False)
                except np.linalg.LinAlgError:
                    continue
                n2 = Vt[2]
                if float(np.abs((Q - cen2) @ n2).max()) > narrow_tol:
                    continue
                if float(np.abs(n2 @ nrm_)) < math.cos(math.radians(ang_max_deg)):
                    continue
            if len(cverts[a_]) < len(cverts[c_]):
                a_, c_ = c_, a_
            parent[c_] = a_
            csum_n[a_] = ns
            csum_c[a_] = csum_c[a_] + csum_c[c_]
            carea[a_] += carea[c_]
            cverts[a_] |= cverts[c_]
            cverts[c_] = set()
    return np.array([find(i) for i in range(nF)])


def _unify(shape, unify_edges: bool, unify_faces: bool, lin: float, ang_deg: float,
           keep=None):
    u = ShapeUpgrade_UnifySameDomain(shape, unify_edges, unify_faces, True)
    u.SetLinearTolerance(lin)
    u.SetAngularTolerance(math.radians(ang_deg))
    if hasattr(u, "SetSafeInputMode"):
        u.SetSafeInputMode(True)
    if keep:
        for s in keep:
            u.KeepShape(s)
    # ⚠️ Con tolleranze larghe (es. -a 0.05) UnifySameDomain puo' fallire
    # ("Courbes non jointives") nel fondere spigoli quasi collineari: in quel
    # caso si tiene la shape com'era, che e' comunque valida.
    try:
        u.Build()
        out = u.Shape()
    except Exception as e:
        Log.warn(f"UnifySameDomain fallita ({e}): passo saltato, shape invariata.")
        return shape
    return shape if (out is None or out.IsNull()) else out


def refit_face_planes(shape, lin_tol: float) -> int:
    """
    ⚠️ UnifySameDomain da' alla faccia fusa il piano del PRIMO triangolo: su
    un triangolo minuscolo la normale float32 sbaglia di 0.03 gradi e a 4 mm di
    distanza i vertici escono di 2e-3 mm. Qui ogni faccia planare con piu' di
    3 vertici riceve il piano ai minimi quadrati dei SUOI vertici (sul posto,
    stessi spigoli, stesso verso).
    """
    b = BRep_Builder()
    n = 0
    for f in explore(shape, TopAbs_FACE):
        f = td_Face(f)
        ad = BRepAdaptor_Surface(f, True)
        if ad.GetType() != GeomAbs_Plane:
            continue
        V = face_vertices(f)
        if len(V) < 4:
            continue
        c = V.mean(axis=0)
        _, _, Vt = np.linalg.svd(V - c, full_matrices=False)
        nrm = Vt[-1]
        d0 = ad.Plane().Axis().Direction()
        old = np.array([d0.X(), d0.Y(), d0.Z()])
        if float(nrm @ old) < 0:
            nrm = -nrm
        dev_new = float(np.abs((V - c) @ nrm).max())
        p0 = ad.Plane().Location()
        dev_old = float(np.abs((V - np.array([p0.X(), p0.Y(), p0.Z()])) @ old).max())
        if dev_new >= dev_old:
            continue
        loc = TopLoc_Location()
        surf = bt_Surface(f, loc)
        if not loc.IsIdentity():
            continue
        b.UpdateFace(f, Geom_Plane(_mk_pnt(c), _mk_dir(nrm)), loc, 1e-7)
        n += 1
    return n


def removable_vertices(shape, lin_tol: float):
    """
    Vertici che stanno su uno spigolo DRITTO fra due sole facce e sono
    allineati (entro lin_tol) con la catena: quelli che UnifySameDomain puo'
    togliere. Il criterio e' la distanza dalla corda dell'intero tratto
    accorpato, non l'angolo fra segmenti consecutivi (che su segmenti corti e'
    solo rumore float32 e su segmenti lunghi deforma).
    Ritorna (vertici da BLOCCARE, numero di rimovibili).
    """
    ef, fmap, e_faces = face_edges_map(shape)
    ve = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, TopAbs_VERTEX, ve)
    nE, nV = _size(ef), _size(ve)
    edges = [td_Edge(ef.FindKey(k)) for k in range(1, nE + 1)]
    fkey = [tuple(sorted(fs)) for fs in e_faces]
    is_line = [BRepAdaptor_Curve(e).GetType() == GeomAbs_Line for e in edges]
    vp = np.array([vpos(ve.FindKey(j)) for j in range(1, nV + 1)]).reshape(-1, 3)
    e_verts = [(ve.FindIndex(te_FirstVertex(e)) - 1, ve.FindIndex(te_LastVertex(e)) - 1)
               for e in edges]
    v_edges = [set() for _ in range(nV)]
    for k, (a_, b_) in enumerate(e_verts):
        for j in (a_, b_):
            if j >= 0:
                v_edges[j].add(k)
    v_edges = [sorted(s) for s in v_edges]
    cand = set()
    for j in range(nV):
        es = v_edges[j]
        if len(es) == 2 and is_line[es[0]] and is_line[es[1]] \
                and fkey[es[0]] == fkey[es[1]] and len(fkey[es[0]]) == 2:
            cand.add(j)

    def other(e, j):
        a_, c_ = e_verts[e]
        return c_ if a_ == j else a_

    removable = set()
    seen = set()
    for j in sorted(cand):
        if j in seen:
            continue
        seen.add(j)
        chain = [j]
        for k_dir, e0 in enumerate(v_edges[j]):
            cur, e = j, e0
            while True:
                nxt = other(e, cur)
                if k_dir == 0:
                    chain.append(nxt)
                else:
                    chain.insert(0, nxt)
                if nxt not in cand or nxt in seen:
                    break
                seen.add(nxt)
                es = [x for x in v_edges[nxt] if x != e]
                if not es:
                    break
                cur, e = nxt, es[0]
        if len(chain) < 3:
            continue
        start, k = 0, 1
        while k < len(chain) - 1:
            P0, P1 = vp[chain[start]], vp[chain[k + 1]]
            d = P1 - P0
            L = float(np.linalg.norm(d))
            ok = L > 1e-12
            if ok:
                d /= L
                q = vp[chain[start + 1:k + 1]] - P0
                ok = bool(np.linalg.norm(q - np.outer(q @ d, d), axis=1).max() <= lin_tol)
            if ok:
                for m in chain[start + 1:k + 1]:
                    removable.add(m)
                k += 1
            else:
                start = k
                k += 1
    keep = [ve.FindKey(j + 1) for j in range(nV) if j not in removable]
    return keep, len(removable)


def recompute_tolerances(shape) -> float:
    """
    Tolleranze RICALCOLATE dalla geometria vera (vertice-piano, vertice-curva,
    curva-piano) invece di quelle gonfiate da UnifySameDomain. Impostate, non
    solo alzate.
    """
    emap, fmap, e_faces = face_edges_map(shape)
    vmap = TopTools_IndexedMapOfShape()
    te_MapShapes(shape, TopAbs_VERTEX, vmap)
    nV, nE, nF = _size(vmap), _size(emap), _size(fmap)
    planes = []
    for i in range(1, nF + 1):
        ad = BRepAdaptor_Surface(td_Face(fmap.FindKey(i)), True)
        if ad.GetType() == GeomAbs_Plane:
            p0 = ad.Plane().Location(); d0 = ad.Plane().Axis().Direction()
            planes.append((np.array([p0.X(), p0.Y(), p0.Z()]), np.array([d0.X(), d0.Y(), d0.Z()])))
        else:
            planes.append(None)
    v_faces = [set() for _ in range(nV)]
    v_edges = [set() for _ in range(nE and nV)]
    e_verts = []
    for k in range(1, nE + 1):
        e = td_Edge(emap.FindKey(k))
        a_ = vmap.FindIndex(te_FirstVertex(e)) - 1
        b_ = vmap.FindIndex(te_LastVertex(e)) - 1
        e_verts.append((a_, b_))
        for j in (a_, b_):
            if j >= 0:
                v_edges[j].add(k - 1)
                for i in e_faces[k - 1]:
                    v_faces[j].add(i)
    curves = []
    for k in range(1, nE + 1):
        e = td_Edge(emap.FindKey(k))
        try:
            c = bt_Curve(e, 0.0, 0.0)
            t0, t1 = bt_Range(e)
            curves.append((c, float(t0), float(t1)))
        except Exception:
            curves.append(None)
    worst = 0.0
    vtol = [1e-7] * nV
    for j in range(nV):
        v = td_Vertex(vmap.FindKey(j + 1))
        P = vpos(v)
        d = 0.0
        for i in v_faces[j]:
            pl = planes[i]
            if pl:
                d = max(d, abs(float((P - pl[0]) @ pl[1])))
        for k in v_edges[j]:
            cv = curves[k]
            if cv is None:
                continue
            c, t0, t1 = cv
            ends = []
            for t in (t0, t1):
                q = c.Value(t)
                ends.append(float(np.linalg.norm(P - np.array([q.X(), q.Y(), q.Z()]))))
            d = max(d, min(ends))
        tol = max(1e-7, 1.2 * d + 1e-9)
        # ⚠️ vicino a facce CURVE la tolleranza serve alle pcurve: mai abbassarla
        if any(planes[i] is None for i in v_faces[j]):
            tol = max(tol, float(bt_Tolerance(v)))
        set_tolerance(v, tol)
        vtol[j] = tol
        worst = max(worst, tol)
    for k in range(nE):
        e = td_Edge(emap.FindKey(k + 1))
        tol = 1e-7
        for j in e_verts[k]:
            if j >= 0:
                tol = max(tol, vtol[j])
        cv = curves[k]
        if cv is not None:
            c, t0, t1 = cv
            ts = np.linspace(t0, t1, 5)
            Q = np.array([[c.Value(t).X(), c.Value(t).Y(), c.Value(t).Z()] for t in ts])
            for i in e_faces[k]:
                pl = planes[i]
                if pl:
                    tol = max(tol, 1.2 * float(np.abs((Q - pl[0]) @ pl[1]).max()) + 1e-9)
        if any(planes[i] is None for i in e_faces[k]):
            tol = max(tol, float(bt_Tolerance(e)))
        set_tolerance(e, tol)
        worst = max(worst, tol)
    return worst


def phase_a(shape, lin_tol: Optional[float] = None, ang_tol_deg: float = 0.005,
            validate: bool = False, title: str = "FASE A — unione facce complanari"):
    Log.banner(title)
    before = shape_stats(shape)
    Log.info(f"Ingresso : {before['faces']:,} facce · {before['edges']:,} edge · "
             f"{before['verts']:,} vertici · {before['solids']} solid")
    free0 = count_free_edges(shape)
    t0 = time.perf_counter()

    V, fv, nrm, areas, emap, fmap, e_faces = face_arrays(shape)
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0))) if len(V) else 1.0
    if lin_tol is None:
        lin_tol = max(1e-5, 2e-6 * diag)
    lab = planar_clusters(V, fv, nrm, areas, e_faces, lin_tol)
    ncl = len(np.unique(lab))
    keep = []
    for k, fs in enumerate(e_faces):
        if len(fs) != 2 or lab[fs[0]] != lab[fs[1]]:
            keep.append(emap.FindKey(k + 1))
    Log.info(f"Gruppi planari (vertici entro {lin_tol:.1e} mm dal piano): {ncl:,} "
             f"· spigoli bloccati {len(keep):,}   [{time.perf_counter()-t0:.2f}s]")
    merged = _unify(shape, False, True, lin_tol, 6.0, keep=keep)
    nfix = refit_face_planes(merged, lin_tol)
    keep_v, nrem = removable_vertices(merged, lin_tol)
    Log.info(f"Piani ri-fittati {nfix:,} · vertici collineari rimovibili {nrem:,}")
    merged = _unify(merged, True, False, lin_tol, 30.0, keep=keep_v)

    # ⚠️ UnifySameDomain lascia le facce fuse col wire marcato "UnorientableShape".
    # ShapeFix_Shape lo sistema sul posto senza creare spigoli nuovi.
    try:
        sf = ShapeFix_Shape(merged)
        sf.SetPrecision(1e-7)
        sf.SetMaxTolerance(max(lin_tol, 1e-6))
        sf.Perform()
        fixed = sf.Shape()
        if fixed is not None and not fixed.IsNull():
            merged = fixed
    except Exception as e:
        Log.debug(f"ShapeFix dopo Unify saltato: {e}")
    worst = recompute_tolerances(merged)
    merged = ensure_solid(merged)
    dt = time.perf_counter() - t0

    after = shape_stats(merged)
    free1 = count_free_edges(merged)
    red_f = 100.0 * (1 - after["faces"] / max(1, before["faces"]))
    Log.ok(f"Uscita   : {after['faces']:,} facce · {after['edges']:,} edge · "
           f"{after['verts']:,} vertici   [{dt:.2f}s]   riduzione facce -{red_f:.1f}%")
    Log.info(f"Spigoli liberi: {free1:,} (in ingresso {free0:,}) · "
             f"tolleranza max ricalcolata {worst:.1e} mm")
    if free1 > free0:
        Log.warn("La Fase A ha aperto il guscio in qualche punto (mesh non manifold li').")
    if validate:
        ok = is_valid(merged)
        (Log.ok if ok else Log.warn)(f"BRepCheck dopo Fase A: {'OK' if ok else 'NON valida'}")
    return merged, before, after


# =============================================================================
# 5. PRIMITIVE: fit algebrico + raffinamento non lineare (dal motore precedente)
# =============================================================================

def taubin_circle(x: np.ndarray, y: np.ndarray,
                  iters: int = 40, eps: float = 1e-12) -> Tuple[float, float, float]:
    """Fit di cerchio ai minimi quadrati (Taubin). Ritorna (cx, cy, r)."""
    n = x.size
    if n < 3:
        raise ValueError("servono almeno 3 punti")
    mx, my = x.mean(), y.mean()
    u, v = x - mx, y - my
    z = u * u + v * v

    Mz = z.mean()
    Mxy = (u * v).mean(); Mxx = (u * u).mean(); Myy = (v * v).mean()
    Mxz = (u * z).mean(); Myz = (v * z).mean(); Mzz = (z * z).mean()

    Cov_xy = Mxx * Myy - Mxy * Mxy
    Var_z = Mzz - Mz * Mz

    A3 = 4.0 * Mz
    A2 = -3.0 * Mz * Mz - Mzz
    A1 = Var_z * Mz + 4.0 * Cov_xy * Mz - Mxz * Mxz - Myz * Myz
    A0 = (Mxz * (Mxz * Myy - Myz * Mxy)
          + Myz * (Myz * Mxx - Mxz * Mxy)
          - Var_z * Cov_xy)
    A22, A33 = 2.0 * A2, 3.0 * A3

    xn, yn = 0.0, 1e30
    for _ in range(iters):
        yo = yn
        yn = A0 + xn * (A1 + xn * (A2 + xn * A3))
        if abs(yn) > abs(yo):
            xn = 0.0
            break
        dy = A1 + xn * (A22 + xn * A33)
        if abs(dy) < eps:
            break
        xo, xn = xn, xn - yn / dy
        if xn == 0.0 or abs((xn - xo) / xn) < eps:
            break
        if xn < 0.0:
            xn = 0.0
            break

    det = xn * xn - xn * Mz + Cov_xy
    if abs(det) < eps:                                   # degenere -> Kasa
        A = np.column_stack([u, v, np.ones(n)])
        sol, *_ = np.linalg.lstsq(A, z, rcond=None)
        cx, cy = sol[0] / 2.0, sol[1] / 2.0
        r = math.sqrt(max(0.0, sol[2] + cx * cx + cy * cy))
        return cx + mx, cy + my, r

    cx = (Mxz * (Myy - xn) - Myz * Mxy) / det / 2.0
    cy = (Myz * (Mxx - xn) - Mxz * Mxy) / det / 2.0
    r = math.sqrt(max(0.0, cx * cx + cy * cy + Mz))
    return cx + mx, cy + my, r


def ortho_frame(axis: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    tmp = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(axis, tmp); u /= np.linalg.norm(u)
    v = np.cross(axis, u)
    return u, v


# --- 5.2  primitive geometriche ---------------------------------------------

PLANE, AXIAL, SPHERE, TORUS = "piano", "assiale", "sfera", "toro"


@dataclass
class Prim:
    """
    kind == PLANE  : center (punto), axis (normale)
    kind == AXIAL  : center (punto sull'asse a t=0), axis, r0, slope
                     raggio(t) = r0 + slope*t   ->  slope==0 cilindro, else cono
    kind == SPHERE : center, r0
    """
    kind: str
    center: np.ndarray
    axis: Optional[np.ndarray] = None
    r0: float = 0.0          # TORUS: raggio MAGGIORE (asse del tubo)
    slope: float = 0.0
    r1: float = 0.0          # TORUS: raggio MINORE (del tubo)
    rms: float = 1e30

    # --- geometria ----------------------------------------------------------
    def _tr(self, P: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Coordinate cilindriche: (t assiale, rho radiale, versore radiale)."""
        d = P - self.center
        t = d @ self.axis
        rad = d - np.outer(t, self.axis)
        rho = np.linalg.norm(rad, axis=1)
        uh = rad / np.maximum(rho, 1e-12)[:, None]
        return t, rho, uh

    def dist(self, P: np.ndarray) -> np.ndarray:
        """Distanza (con segno) dei punti dalla superficie."""
        if self.kind == PLANE:
            return (P - self.center) @ self.axis
        if self.kind == SPHERE:
            return np.linalg.norm(P - self.center, axis=1) - self.r0
        if self.kind == TORUS:
            t, rho, _ = self._tr(P)
            return np.hypot(rho - self.r0, t) - self.r1
        t, rho, _ = self._tr(P)
        # per il cono la distanza vera e' la deviazione radiale * cos(semiangolo)
        return (rho - (self.r0 + self.slope * t)) / math.hypot(1.0, self.slope)

    def normal_at(self, P: np.ndarray) -> np.ndarray:
        """Normale (non orientata) della primitiva nei punti dati."""
        if self.kind == PLANE:
            return np.tile(self.axis, (len(P), 1))
        if self.kind == SPHERE:
            d = P - self.center
            return d / np.maximum(np.linalg.norm(d, axis=1), 1e-12)[:, None]
        if self.kind == TORUS:
            t, rho, uh = self._tr(P)
            n = (rho - self.r0)[:, None] * uh + t[:, None] * self.axis
            return n / np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
        _, _, uh = self._tr(P)
        n = uh - self.slope * self.axis
        return n / np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]

    def label(self) -> str:
        if self.kind == PLANE:
            return "PIANO"
        if self.kind == SPHERE:
            return "SFERA"
        if self.kind == TORUS:
            return "TORO"
        return "CILIND" if abs(self.slope) < 1e-3 else "CONO"


# --- 5.3  fit delle primitive ------------------------------------------------

def fit_plane(P: np.ndarray) -> Optional[Prim]:
    if len(P) < 3:
        return None
    c = P.mean(axis=0)
    _, _, Vt = np.linalg.svd(P - c, full_matrices=False)
    n = Vt[-1]
    p = Prim(PLANE, c, n / np.linalg.norm(n))
    p.rms = float(np.sqrt(np.mean(p.dist(P) ** 2)))
    return p


def fit_sphere(P: np.ndarray) -> Optional[Prim]:
    if len(P) < 4:
        return None
    A = np.column_stack([2.0 * P, np.ones(len(P))])
    b = (P ** 2).sum(axis=1)
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    c = sol[:3]
    rr = sol[3] + c @ c
    if not np.isfinite(rr) or rr <= 1e-12:
        return None
    p = Prim(SPHERE, c, None, math.sqrt(rr))
    p.rms = float(np.sqrt(np.mean(p.dist(P) ** 2)))
    return p


def _cone_algebraic(px, py, t, max_slope: float):
    """
    Cono con asse gia' noto, in forma chiusa.

    ⚠️ IL CERCHIO UNICO NON VA BENE. Proiettando un cono sul piano ortogonale
    all'asse i punti stanno su cerchi di raggio DIVERSO (uno per ogni quota):
    fitne uno solo sposta il centro, i raggi escono sbagliati e la conicita'
    stimata per regressione e' fuori di un ordine di grandezza (-0.07 invece
    di -1). Qui si usa l'equazione del cono
        (x-cx)^2 + (y-cy)^2 = (r0 + s t)^2
    che, sviluppata, e' LINEARE nelle incognite (cx, cy, A, B, C) con
    A = cx^2+cy^2-r0^2, B = 2 r0 s, C = s^2. Da C e B si ricavano conicita' e
    raggio, segno compreso.
    """
    if len(t) < 6 or float(t.max() - t.min()) < 1e-9:
        return None
    M = np.column_stack([2.0 * px, 2.0 * py, -np.ones_like(px), t, t * t])
    rhs = px * px + py * py
    try:
        sol, *_ = np.linalg.lstsq(M, rhs, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy, A, B, C = (float(x) for x in sol)
    if not all(np.isfinite(v) for v in (cx, cy, A, B, C)) or C < 0.0:
        return None
    s = math.sqrt(C)
    if s < 1e-9:
        return None
    if B < 0:
        s = -s
    r0 = 0.5 * B / s
    if not np.isfinite(r0) or r0 <= 1e-9 or abs(s) > max_slope:
        return None
    return cx, cy, r0, s


def _cone_rings(px, py, t, max_slope: float):
    """
    Cono con asse noto, ricavato dagli ANELLI.

    ⚠️ Una fascia tassellata con UNA SOLA FILA di faccette ha i vertici su due
    sole quote: la forma algebrica del cono diventa singolare (con due valori
    di t la colonna t^2 e' combinazione lineare di t e della costante) e la
    conicita' esce a caso. Pero' quei due anelli sono proprio cio' che serve:
    un cerchio per anello da' centro e raggio, e due raggi a due quote danno
    la conicita' esatta.
    """
    n = len(t)
    if n < 6:
        return None
    span = float(t.max() - t.min())
    if span < 1e-9:
        return None
    order = np.argsort(t)
    groups, cur = [], [int(order[0])]
    for i in order[1:]:
        i = int(i)
        if t[i] - t[cur[-1]] > 0.05 * span:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    groups.append(cur)
    groups = [g for g in groups if len(g) >= 3]
    if len(groups) < 2:
        return None
    cs = []
    for g in groups:
        try:
            cx, cy, _ = taubin_circle(px[g], py[g])
        except Exception:
            continue
        if np.isfinite(cx) and np.isfinite(cy):
            cs.append((cx, cy, len(g)))
    if len(cs) < 2:
        return None
    wsum = sum(k for _, _, k in cs)
    cx = sum(a * k for a, _, k in cs) / wsum
    cy = sum(b * k for _, b, k in cs) / wsum
    tt = np.array([float(t[g].mean()) for g in groups])
    rr = np.array([float(np.hypot(px[g] - cx, py[g] - cy).mean()) for g in groups])
    if len(tt) == 2:
        slope = float((rr[1] - rr[0]) / (tt[1] - tt[0]))
        r0 = float(rr[0] - slope * tt[0])
    else:
        slope, r0 = (float(x) for x in np.polyfit(tt, rr, 1))
    if not (np.isfinite(r0) and np.isfinite(slope)) or abs(slope) > max_slope:
        return None
    return cx, cy, r0, slope


def _axial_on_axis(P: np.ndarray, axis: np.ndarray, force_cyl: bool,
                   max_slope: float) -> Optional[Prim]:
    """Raggio e conicita' ai minimi quadrati attorno a un asse dato."""
    O = P.mean(axis=0)
    u, v = ortho_frame(axis)
    Q = P - O
    px, py, t = Q @ u, Q @ v, Q @ axis
    best = None
    variants = []
    try:
        cx, cy, _ = taubin_circle(px, py)
        if np.isfinite(cx) and np.isfinite(cy):
            rho = np.hypot(px - cx, py - cy)
            if (not force_cyl) and t.max() - t.min() > 1e-9 and len(t) > 3:
                slope, r0 = np.polyfit(t, rho, 1)
            else:
                slope, r0 = 0.0, float(rho.mean())
            variants.append((cx, cy, float(r0), float(slope)))
    except Exception:
        pass
    if not force_cyl:
        for fn in (_cone_algebraic, _cone_rings):
            alt = fn(px, py, t, max_slope)
            if alt is not None:
                variants.append(alt)
    tm = 0.5 * float(t.min() + t.max())
    for cx, cy, r0, slope in variants:
        if not np.isfinite(r0) or abs(slope) > max_slope:
            continue
        # origine al centro dei dati: cosi' il raggio di riferimento e' quello
        # vero della fascia, non quello (magari negativo) al vertice del cono
        cen = O + cx * u + cy * v + tm * axis
        rm = float(r0 + slope * tm)
        if not np.isfinite(rm) or rm <= 1e-6:
            continue
        q = Prim(AXIAL, cen, axis, rm, float(slope))
        q.rms = float(np.sqrt(np.mean(q.dist(P) ** 2)))
        if best is None or q.rms < best.rms:
            best = q
    return best


def fit_axial(P: np.ndarray, N: np.ndarray, W: np.ndarray,
              max_slope: float = 3.0, ratio_1d: float = 0.05) -> Optional[Prim]:
    """
    Cilindro o cono.

    Le normali soddisfano  n·a = s  costante  (s = 0 cilindro, s = sin(alfa) cono):
    cioe' i punti {n_i} nello spazio delle normali stanno su un PIANO. Il fit e'
    quindi l'autovettore minimo della covarianza delle normali CENTRATE.
    Centrare e' obbligatorio: senza, archi parziali e coni sbagliano asse.

    ⚠️ CASO DEGENERE. Un intorno PICCOLO di cilindro ha le normali quasi
    COLLINEARI (stanno su un arco cortissimo). Infiniti piani contengono una
    retta, quindi l'autovettore minimo e' arbitrario e l'asse esce a caso.
    In quel caso si risolve la degenerazione scegliendo l'interpretazione
    cilindro (s = 0): l'asse deve essere ortogonale sia alla normale media sia
    alla direzione di spread, quindi  a = n_medio x w.

    ⚠️ MA NON SEMPRE. Uno SMUSSO CURVO (cono a 45 gradi attorno a uno spigolo
    arrotondato) ha anche lui le normali su un arco corto, eppure il suo asse
    e' determinato benissimo: la componente delle normali lungo l'asse e'
    COSTANTE, e l'autovettore minimo la trova. Se si forza il cilindro, l'asse
    esce ortogonale a quello vero e il fit sbaglia di centesimi; poi una sfera
    passante per i due cerchi di bordo "spiega" la fascia meglio del finto
    cilindro, e al posto dello smusso si ritrova una calotta. Quindi nel caso
    degenere si provano ENTRAMBE le interpretazioni e si tiene quella che
    aderisce di piu' ai punti.
    """
    if len(P) < 6 or len(N) < 3:
        return None
    w = W / max(W.sum(), 1e-300)
    nb = (N * w[:, None]).sum(axis=0)
    D = N - nb
    M = (D * w[:, None]).T @ D
    evals, evecs = np.linalg.eigh(M)

    if evals[2] <= 1e-16:
        return None                                   # normali tutte identiche

    cands = []
    if evals[1] <= ratio_1d * evals[2]:
        # --- spread 1D: degenere ---
        spread = evecs[:, 2]
        a = np.cross(nb, spread)
        na = np.linalg.norm(a)
        if na > 1e-9:
            cands.append((a / na, True))              # interpretazione cilindro
        n0 = np.linalg.norm(evecs[:, 0])
        if n0 > 1e-9:
            cands.append((evecs[:, 0] / n0, False))   # interpretazione cono
    else:
        # --- spread 2D: il fit di piano nello spazio delle normali e' valido ---
        if evals[0] / evals[1] > 0.15:
            return None                               # spread 3D -> sfera, non assiale
        n0 = np.linalg.norm(evecs[:, 0])
        if n0 < 1e-9:
            return None
        cands.append((evecs[:, 0] / n0, False))

    best = None
    for axis, force_cyl in cands:
        q = _axial_on_axis(P, axis, force_cyl, max_slope)
        if q is not None and (best is None or q.rms < best.rms):
            best = q
    return best


def fit_axial_rev(P: np.ndarray, Nrep: np.ndarray, Wp: np.ndarray) -> Optional[Prim]:
    """
    Cono/cilindro con l'asse preso da revolution_axis invece che dalla
    covarianza delle normali.

    ⚠️ SMUSSO CURVO, UNA SOLA FILA DI FACCETTE. Uno smusso a 45 gradi attorno
    a uno spigolo arrotondato e' un CONO, ma tassellato ha i vertici solo sui
    due cerchi di bordo e le normali su un arco corto: la covarianza delle
    normali e' degenere (spread 1D), fit_axial ripiega sul cilindro e
    sbaglia l'asse di brutto. Allora la stessa superficie viene "spiegata"
    benissimo anche da una SFERA che passa per i due cerchi, e il pezzo si
    riempie di calotte sferiche al posto degli smussi. Le posizioni pero'
    l'informazione ce l'hanno: n . (a x (p - c)) = 0 e' lineare in (a, a x c)
    e da' l'asse senza casi degeneri. Da li' bastano due minimi quadrati per
    raggio e conicita'. Il verdetto finale lo da' comunque la deviazione
    delle normali, che sul cono e' un terzo di quella della sfera.
    """
    ra = revolution_axis(P, Nrep, Wp)
    if ra is None:
        return None
    a, c = ra
    na = float(np.linalg.norm(a))
    if na < 1e-9:
        return None
    a = a / na
    t = (P - c) @ a
    rho = np.linalg.norm((P - c) - np.outer(t, a), axis=1)
    if t.max() - t.min() < 1e-9 or len(t) < 4:
        return None
    try:
        slope, r0 = np.polyfit(t, rho, 1)
    except Exception:
        return None
    if not (np.isfinite(r0) and np.isfinite(slope)) or abs(slope) > 3.0:
        return None
    # ⚠️ il punto di riferimento dell'asse puo' cadere oltre il vertice del
    # cono, e li' il "raggio a t=0" e' NEGATIVO: non e' un fit sbagliato, e'
    # solo un'origine scomoda. Si riporta l'origine in mezzo ai dati.
    tm = 0.5 * float(t.min() + t.max())
    c = c + tm * a
    r0 = float(r0 + slope * tm)
    if r0 <= 1e-6:
        return None
    q = Prim(AXIAL, c, a, float(r0), float(slope))
    q.rms = float(np.sqrt(np.mean(q.dist(P) ** 2)))
    return q


def revolution_axis(P: np.ndarray, N: np.ndarray, W: np.ndarray):
    """
    Asse di una superficie di rivoluzione, in forma chiusa.

    Per QUALSIASI superficie di rivoluzione (cilindro, cono, sfera, toro) la
    normale in un punto non ha componente tangenziale:  n . (a x (p - c)) = 0.
    Scritta come  a.(p x n) = (a x c).n , e' LINEARE e omogenea in (a, a x c):
    l'asse esce dalla SVD, senza inneschi ne' casi degeneri. E' molto piu'
    robusta della covarianza delle normali su strisce sottili.
    """
    if len(P) < 6:
        return None
    A = np.column_stack([np.cross(P, N), -N]) * np.sqrt(np.maximum(W, 1e-12))[:, None]
    try:
        _, _, Vt = np.linalg.svd(A, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    m, g = Vt[-1][:3], Vt[-1][3:]
    nm = float(np.linalg.norm(m))
    if nm < 1e-9:
        return None
    m = m / nm
    return m, -np.cross(m, g / nm)


def fit_torus(P: np.ndarray, N: np.ndarray, W: np.ndarray) -> Optional[Prim]:
    """
    Toro: il RACCORDO LUNGO UNO SPIGOLO CURVO.

    ⚠️ Senza questa primitiva un pezzo meccanico vero non si converte. Ogni
    smusso o raccordo attorno a un foro, a un mozzo o a un bordo arrotondato e'
    un TORO, non un cilindro: sui suoi triangoli il miglior cilindro sbaglia di
    un decimo di millimetro e la regione viene espulsa a ogni passata. Restano
    tassellati proprio i bordi tondi, che sono quelli che si vedono.
    Nel piano meridiano (rho, z) il profilo del toro e' un CERCHIO: asse dalla
    SVD, poi cerchio di Taubin sul profilo.
    """
    q = revolution_axis(P, N, W)
    if q is None:
        return None
    a, c = q
    d = P - c
    z = d @ a
    rho = np.linalg.norm(d - np.outer(z, a), axis=1)
    try:
        R, z0, r = taubin_circle(rho, z)
    except Exception:
        return None
    if not (np.isfinite(R) and np.isfinite(r) and np.isfinite(z0)):
        return None
    if r <= 1e-9 or R <= 1e-9:
        return None
    if R < 0.15 * r or R > 60.0 * r:
        return None                      # degenera in sfera o in cilindro
    p = Prim(TORUS, c + z0 * a, a, float(R), 0.0, float(r))
    p.rms = float(np.sqrt(np.mean(p.dist(P) ** 2)))
    return p


def normal_deviation(prim: Prim, P: np.ndarray, Nrep: np.ndarray) -> float:
    """
    Scarto angolare medio (gradi) fra le normali delle facce e la normale che
    la primitiva prevede nei loro baricentri.

    ⚠️ E' IL DISCRIMINANTE PRINCIPALE, piu' della distanza dei punti.
    Esempio reale: una parete di foro tassellata in strisce alte quanto tutto
    il pezzo ha i vertici solo sui due bordi. Quei punti giacciono ESATTAMENTE
    sia su un cilindro sia su una sfera (r = sqrt(50^2+250^2)), e il residuo
    posizionale non sa decidere: vince il rumore in virgola mobile. Le normali
    invece sono orizzontali, mentre la sfera le vorrebbe inclinate di 79 gradi.

    ⚠️ Si valuta sui VERTICI, non sul baricentro della faccia. Nel caso sopra
    il baricentro della striscia cade sull'equatore della sfera, dove anche la
    sfera ha normale orizzontale: il confronto li' non distingue nulla. Sui
    vertici (ai due bordi) la sfera sbaglia di 79 gradi e viene scartata.
    """
    if len(P) == 0:
        return 0.0
    pn = prim.normal_at(P)
    d = np.abs(np.einsum("ij,ij->i", pn, Nrep))
    return float(np.degrees(np.arccos(np.clip(d, -1.0, 1.0))).mean())


def rank_primitives(P, Nrep, N, W, allow_sphere=True, allow_cone=True,
                    flat_slope: float = 1e-3, max_ndev: float = 25.0,
                    allow_torus: bool = True) -> List[Prim]:
    """
    Tutte le primitive plausibili, ORDINATE per residuo (penalita' progressiva
    per preferire i modelli piu' semplici).

    ⚠️ Ritorna una LISTA, non il solo vincitore. Su un intorno piccolo il
    residuo e' quasi identico per primitive diverse: una parete di foro puo'
    sembrare una sfera. Se la prima scelta cresce male, il chiamante deve
    poter ripiegare sulla seconda invece di sprecare le facce.
    """
    cands = []
    pl = fit_plane(P)
    if pl:
        cands.append((pl.rms * 1.00, pl))
    ax = fit_axial(P, N, W)
    span_ = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0))) or 1.0
    if ax is None or ax.rms > 1e-5 * span_:
        # fascia sottile: l'asse dalle normali e' degenere, si prova quello
        # di rivoluzione (vedi fit_axial_rev)
        alt = fit_axial_rev(P, Nrep, np.ones(len(P)))
        if alt is not None and (ax is None or alt.rms < ax.rms):
            ax = alt
    if ax:
        if abs(ax.slope) < flat_slope:
            ax.slope = 0.0
            cands.append((ax.rms * 1.06, ax))
        elif allow_cone:
            cands.append((ax.rms * 1.15, ax))
    if allow_sphere:
        sp = fit_sphere(P)
        if sp:
            cands.append((sp.rms * 1.10, sp))
    # ⚠️ il toro va fittato sui punti, non sulle facce: servono le normali
    #    ripetute per vertice (Nrep), non una per faccia.
    tr = fit_torus(P, Nrep, np.ones(len(P))) if allow_torus else None
    if tr:
        cands.append((tr.rms * 1.25, tr))   # penalita': vince solo se serve
    keep = []
    for sc, pr in cands:
        nd = normal_deviation(pr, P, Nrep)
        if nd <= max_ndev:
            keep.append((sc, pr))
    keep.sort(key=lambda c: c[0])
    return [c[1] for c in keep]


_ALIVE: List[object] = []   # builder OCC da tenere vivi (SWIG/pybind: riferimenti)


def _keep(obj):
    _ALIVE.append(obj)
    return obj


_EDGE_ERR = ("ok", "proiezione del punto fallita", "parametro fuori intervallo",
             "punti diversi su curva chiusa", "parametro infinito",
             "punto e parametro incoerenti", "retta per punti coincidenti")


def _mk_vertex(p: np.ndarray, tol: float):
    """
    Vertice con tolleranza ESPLICITA.
    BRepBuilderAPI_MakeVertex usa Precision::Confusion() = 1e-7 mm. I nodi
    ricalcolati stanno sulle curve a meno del rumore della mesh (~1e-5 mm su un
    pezzo da 500 mm): con 1e-7 ogni MakeEdge fallisce con "punto e parametro
    incoerenti". La tolleranza va dimensionata sullo scarto vero.
    """
    v = TopoDS_Vertex()
    BRep_Builder().MakeVertex(v, _mk_pnt(p), float(max(tol, 1e-7)))
    return v


def _mk_dir(v: np.ndarray):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("direzione nulla")
    v = v / n
    return gp_Dir(float(v[0]), float(v[1]), float(v[2]))


def _mk_pnt(p: np.ndarray):
    p = np.asarray(p, dtype=float)
    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]))


# --- 5C.1  raffinamento non lineare delle primitive (Levenberg-Marquardt) ----
#
# ⚠️ E' QUI CHE NASCEVA IL DISASTRO PRECEDENTE.
# Il fit algebrico (covarianza delle normali + Taubin) e' solo un INNESCO: su
# un arco di 90 gradi sbaglia il raggio di 0.1 mm e l'asse di 1e-4 rad. Con
# quell'errore il cilindro non e' piu' tangente al piano e ogni booleana
# degenera. La minimizzazione vera della distanza ortogonale porta il residuo
# al livello del rumore della mesh (1e-5 mm), cioe' al valore nominale esatto.

LM_MAX_PTS = 900

def lm_refine(prim: "Prim", P: np.ndarray, W: Optional[np.ndarray] = None,
              iters: int = 80) -> "Prim":
    """Minimizza la distanza ortogonale punti-superficie. Ritorna una Prim nuova."""
    if prim is None or len(P) < 4:
        return prim
    if prim.kind == PLANE:
        return prim                       # SVD e' gia' ottimale per il piano
    P_all = P
    if len(P) > LM_MAX_PTS:               # su regioni enormi il campione basta
        sel = np.linspace(0, len(P) - 1, LM_MAX_PTS).astype(int)
        P = P[sel]
        if W is not None:
            W = W[sel]

    w = np.ones(len(P)) if W is None else np.sqrt(np.maximum(W, 1e-12))
    w = w / w.mean()

    if prim.kind == SPHERE:
        c0 = prim.center.astype(float).copy()
        x = np.array([0.0, 0.0, 0.0, float(prim.r0)])

        def build(v):
            return Prim(SPHERE, c0 + v[:3], None, float(v[3]))
    elif prim.kind == TORUS:
        a0 = prim.axis / np.linalg.norm(prim.axis)
        u0, v0 = ortho_frame(a0)
        c0 = prim.center.astype(float).copy()
        x = np.array([0.0, 0.0, 0.0, 0.0, 0.0,
                      float(prim.r0), float(prim.r1)])

        def build(v):
            a = a0 + v[0] * u0 + v[1] * v0
            a = a / np.linalg.norm(a)
            c = c0 + v[2] * u0 + v[3] * v0 + v[4] * a0
            return Prim(TORUS, c, a, float(v[5]), 0.0, float(v[6]))
    else:
        a0 = prim.axis / np.linalg.norm(prim.axis)
        u0, v0 = ortho_frame(a0)
        c0 = prim.center.astype(float).copy()
        is_cyl = abs(prim.slope) < 1e-9
        x = (np.array([0.0, 0.0, 0.0, 0.0, float(prim.r0)]) if is_cyl else
             np.array([0.0, 0.0, 0.0, 0.0, float(prim.r0), float(prim.slope)]))

        def build(v):
            a = a0 + v[0] * u0 + v[1] * v0
            a = a / np.linalg.norm(a)
            c = c0 + v[2] * u0 + v[3] * v0
            return Prim(AXIAL, c, a, float(v[4]), 0.0 if is_cyl else float(v[5]))

    def resid(v):
        try:
            return w * build(v).dist(P)
        except Exception:
            return np.full(len(P), 1e12)

    r = resid(x)
    cost = float(r @ r)
    lam = 1e-3
    m = len(x)
    converged = False
    for _ in range(iters):
        if converged:
            break
        J = np.empty((len(P), m))
        for k in range(m):
            h = 1e-7 * max(1.0, abs(x[k]))
            xp = x.copy(); xp[k] += h
            J[:, k] = (resid(xp) - r) / h
        A = J.T @ J
        g = J.T @ r
        for _try in range(30):
            try:
                dx = np.linalg.solve(A + lam * np.diag(np.maximum(np.diag(A), 1e-12)), -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            xn = x + dx
            rn = resid(xn)
            cn = float(rn @ rn)
            if cn < cost:
                converged = (cost - cn) < 1e-9 * max(cost, 1e-300)
                x, r, cost = xn, rn, cn
                lam = max(lam * 0.3, 1e-12)
                break
            lam *= 10.0
            if lam > 1e10:
                break
        if lam > 1e10:
            break
        if np.linalg.norm(dx) < 1e-14 * (1.0 + np.linalg.norm(x)):
            break

    out = build(x)
    out.rms = float(np.sqrt(np.mean(out.dist(P_all) ** 2)))
    return out


def _refit(region, verts, norms, areas, kind) -> Optional[Prim]:
    idx = list(region)
    P = np.vstack([verts[i] for i in idx])
    N = np.array([norms[i] for i in idx])
    W = np.array([max(areas[i], 1e-9) for i in idx])
    ok = np.einsum("ij,ij->i", N, N) > 0.5
    if kind == PLANE:
        return fit_plane(P)
    if kind == SPHERE:
        return fit_sphere(P)
    if ok.sum() < 3:
        return None
    if kind == TORUS:
        Wp = np.concatenate([np.full(len(verts[i]), max(areas[i], 1e-12))
                             for i in idx])
        Nr = np.vstack([np.tile(norms[i], (len(verts[i]), 1)) for i in idx])
        return fit_torus(P, Nr, Wp)
    return fit_axial(P, N[ok], W[ok])


# --- 5.7  segmentazione completa --------------------------------------------



def refit_exact(faces_idx, verts, norms, areas, kind) -> Optional["Prim"]:
    """_refit() seguito dal raffinamento LM."""
    p = _refit(set(faces_idx), verts, norms, areas, kind)
    P = np.vstack([verts[i] for i in faces_idx])
    Wt = np.concatenate([np.full(len(verts[i]), max(areas[i], 1e-12) / max(len(verts[i]), 1))
                         for i in faces_idx])
    q = lm_refine(p, P, Wt) if p is not None else None
    # ⚠️ seconda strada per cilindri e coni: se il fit dalle normali e' assente
    # o scadente (fascia sottile, normali su un arco corto) si riprova con
    # l'asse di rivoluzione, che su quelle fasce e' l'unico che tiene.
    if kind == AXIAL:
        span = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0))) or 1.0
        dq = float(np.abs(q.dist(P)).max()) if q is not None else math.inf
        if dq > 1e-5 * span:
            Nr = np.vstack([np.tile(norms[i], (len(verts[i]), 1)) for i in faces_idx])
            r = fit_axial_rev(P, Nr, Wt)
            if r is not None:
                r = lm_refine(r, P, Wt)
                if r is not None and float(np.abs(r.dist(P)).max()) < dq:
                    q = r
    if q is None:
        return None
    # ⚠️ CONICITA' SOTTO IL RUMORE: un foro alesato esce spesso come cono con
    # rastremazione di qualche centesimo di micron (ø7.9999-8.0000). Diventa
    # una CONICAL_SURFACE invece di una CYLINDRICAL_SURFACE, e la faccia si
    # comporta male con i vicini. Se la variazione di raggio lungo la regione
    # non supera il residuo del fit, e' rumore: si azzera.
    if q is not None and q.kind == AXIAL and q.slope != 0.0:
        t = (P - q.center) @ q.axis
        span = float(t.max() - t.min())
        if abs(q.slope) * span <= max(2.0 * float(q.rms), 1e-4):
            tm = 0.5 * float(t.max() + t.min())
            q.center = q.center + tm * q.axis
            q.r0 = float(q.r0 + q.slope * tm)
            q.slope = 0.0
    return q



# --- 5C.1b  estrazione GLOBALE delle primitive (RANSAC alla Schnabel) --------
#
# ⚠️ PERCHE' SERVE, E PERCHE' NON BASTA FAR CRESCERE DAI SEMI.
# La crescita da seme decide il tipo di primitiva guardando due anelli di
# triangoli e poi si porta dietro quella scelta. Su un pezzo vero produce
# decine di regioni minuscole (cilindri da 3-5 facce con copertura del 3%):
# sono le "zone troppo dense" che si vedono nel modello convertito.
# Il RANSAC globale ragiona al contrario: propone una primitiva da un pugno di
# facce vicine e poi conta QUANTE facce dell'intero pezzo la sostengono. Vince
# la primitiva col supporto piu' grande, che per costruzione e' quella grande e
# vera, non il frammento. E' l'idea alla base di CGAL Shape Detection
# (Efficient RANSAC di Schnabel), qui in numpy per non aggiungere dipendenze.

def refit_best(faces_idx, verts, norms, areas,
               current_kind: Optional[str] = None) -> Optional["Prim"]:
    """
    Rimette in discussione il TIPO di primitiva, non solo i suoi parametri.

    ⚠️ E' LA RAGIONE PER CUI I RACCORDI TONDI RESTAVANO TASSELLATI.
    Il tipo viene scelto sul SEME, cioe' su due anelli di triangoli: la' piano,
    cilindro, sfera e toro si equivalgono e vince quasi sempre il piu' semplice.
    Poi la regione cresce fino a diventare una fascia toroidale di 300 facce...
    e continua a essere rifittata COME SFERA, perche' _refit conserva il kind.
    Il residuo resta cento volte sopra il gate, la purga la sbriciola a ogni
    passata e quelle facce non diventano mai una superficie.
    Qui, a regione cresciuta, si riprovano TUTTI i tipi e si tiene il migliore
    (con penalita' di complessita': a parita' di residuo vince il piu' semplice).
    """
    idx = list(faces_idx)
    if len(idx) < 3:
        return None
    P = np.vstack([verts[i] for i in idx])
    if P.size == 0:
        return None
    Nrep = np.vstack([np.tile(norms[i], (len(verts[i]), 1)) for i in idx])
    Wp = np.concatenate([np.full(len(verts[i]), max(areas[i], 1e-12))
                         for i in idx])
    N = np.array([norms[i] for i in idx])
    W = np.array([max(areas[i], 1e-9) for i in idx])
    ok = np.einsum("ij,ij->i", N, N) > 0.5

    cands = []
    pl = fit_plane(P)
    if pl is not None:
        cands.append((1.00, pl))
    if ok.sum() >= 3:
        ax = fit_axial(P, N[ok], W[ok])
        if ax is not None:
            cands.append((1.06 if abs(ax.slope) < 1e-3 else 1.15, ax))
    sp = fit_sphere(P)
    if sp is not None:
        cands.append((1.10, sp))
    tr = fit_torus(P, Nrep, Wp)
    if tr is not None:
        cands.append((1.25, tr))
    if not cands:
        return None

    best, bs = None, math.inf
    for pen, p in cands:
        p2 = lm_refine(p, P, Wp)
        if p2 is None:
            continue
        d = p2.dist(P)
        rms = float(np.sqrt(np.mean(d ** 2)))
        # piccolo bonus a chi era gia' il tipo scelto: evita oscillazioni
        s = rms * pen * (0.95 if p2.kind == current_kind else 1.0)
        if s < bs:
            best, bs = p2, s
    return best


# --- 5C.2b  fusione delle regioni co-superficie ------------------------------
#
# ⚠️ SENZA QUESTO IL MODELLO SI SPACCA. La crescita parte da semi diversi e un
# raccordo lungo finisce spesso in DUE regioni con la stessa identica primitiva.
# Diventerebbero due facce cilindriche sovrapposte separate da uno spigolo
# inesistente. Qui le regioni adiacenti che giacciono sulla STESSA superficie
# vengono riunite e rifittate.

def prims_same(pa: "Prim", pb: "Prim", tol_len: float, cos_ang: float) -> bool:
    if pa is None or pb is None or pa.kind != pb.kind:
        return False
    if pa.kind == PLANE:
        if float(pa.axis @ pb.axis) < cos_ang:
            return False
        return abs(float((pb.center - pa.center) @ pa.axis)) <= tol_len
    if pa.kind == SPHERE:
        return (float(np.linalg.norm(pa.center - pb.center)) <= tol_len
                and abs(pa.r0 - pb.r0) <= tol_len)
    if abs(float(pa.axis @ pb.axis)) < cos_ang:
        return False
    d = pb.center - pa.center
    if float(np.linalg.norm(d - float(d @ pa.axis) * pa.axis)) > tol_len:
        return False
    if pa.kind == TORUS:
        return (abs(pa.r0 - pb.r0) <= tol_len and abs(pa.r1 - pb.r1) <= tol_len
                and abs(float(d @ pa.axis)) <= tol_len)
    if abs(pa.slope - abs(pb.slope) * (1.0 if float(pa.axis @ pb.axis) > 0 else -1.0)) > 1e-4:
        return False
    t = float(d @ pa.axis)
    return abs((pa.r0 + pa.slope * t) - pb.r0) <= tol_len



# =============================================================================
# 6. CURVE ANALITICHE e intersezioni superficie-superficie
# =============================================================================
class _Curve:
    period = None

    def param(self, P: np.ndarray) -> np.ndarray:      # pragma: no cover
        raise NotImplementedError

    def point(self, t: np.ndarray) -> np.ndarray:      # pragma: no cover
        raise NotImplementedError

    def dist(self, P: np.ndarray) -> np.ndarray:
        return np.linalg.norm(P - self.point(self.param(P)), axis=1)

    def to_geom(self):                                 # pragma: no cover
        raise NotImplementedError


@dataclass
class CLine(_Curve):
    p: np.ndarray
    d: np.ndarray

    def param(self, P):
        return np.atleast_1d((np.atleast_2d(P) - self.p) @ self.d)

    def point(self, t):
        t = np.atleast_1d(t)
        return self.p + np.outer(t, self.d)

    def to_geom(self):
        return Geom_Line(_mk_pnt(self.p), _mk_dir(self.d))

    def label(self):
        return "retta"


@dataclass
class CCircle(_Curve):
    c: np.ndarray
    n: np.ndarray
    u: np.ndarray
    v: np.ndarray
    r: float
    period = 2.0 * math.pi

    def param(self, P):
        d = np.atleast_2d(P) - self.c
        return np.arctan2(d @ self.v, d @ self.u)

    def point(self, t):
        t = np.atleast_1d(t)
        return self.c + self.r * (np.cos(t)[:, None] * self.u
                                  + np.sin(t)[:, None] * self.v)

    def to_geom(self):
        ax2 = gp_Ax2(_mk_pnt(self.c), _mk_dir(self.n), _mk_dir(self.u))
        return Geom_Circle(ax2, float(self.r))

    def label(self):
        return f"cerchio r={self.r:.4f}"


@dataclass
class CEllipse(_Curve):
    c: np.ndarray
    n: np.ndarray
    u: np.ndarray            # asse MAGGIORE
    v: np.ndarray            # asse minore
    a: float
    b: float
    period = 2.0 * math.pi

    def param(self, P):
        d = np.atleast_2d(P) - self.c
        return np.arctan2((d @ self.v) / max(self.b, 1e-12),
                          (d @ self.u) / max(self.a, 1e-12))

    def point(self, t):
        t = np.atleast_1d(t)
        return self.c + (self.a * np.cos(t))[:, None] * self.u \
                      + (self.b * np.sin(t))[:, None] * self.v

    def to_geom(self):
        ax2 = gp_Ax2(_mk_pnt(self.c), _mk_dir(self.n), _mk_dir(self.u))
        return Geom_Ellipse(ax2, float(self.a), float(self.b))

    def label(self):
        return f"ellisse {self.a:.4f}x{self.b:.4f}"


def _circle(c, n, r) -> CCircle:
    n = n / np.linalg.norm(n)
    u, v = ortho_frame(n)
    return CCircle(np.asarray(c, float), n, u, np.cross(n, u), float(r))


def _ellipse(c, n, u, a: float, b: float) -> Optional[CEllipse]:
    """
    ⚠️ Ortonormalizzazione OBBLIGATORIA prima di costruire l'ellisse.
    Geom_Ellipse pretende asse maggiore >= minore e ricava Y = Z x X: se il
    versore minore passato qui fosse l'opposto, il parametro Python e quello
    OCC avrebbero segno opposto e il trim prenderebbe l'arco sbagliato.
    """
    n = np.asarray(n, float)
    nn = np.linalg.norm(n)
    u = np.asarray(u, float) - 0.0
    if nn < 1e-12:
        return None
    n = n / nn
    u = u - float(u @ n) * n
    nu = np.linalg.norm(u)
    if nu < 1e-12:
        return None
    u = u / nu
    if b > a:
        a, b = b, a
        u = np.cross(n, u)
    return CEllipse(np.asarray(c, float), n, u, np.cross(n, u), float(a), float(b))


# --- 5C.6  intersezione ESATTA fra due primitive -----------------------------

def surf_surf_curves(pa: "Prim", pb: "Prim", tol: float) -> List[_Curve]:
    """
    Curve di intersezione analitiche fra due primitive.
    Ritorna TUTTE le soluzioni possibili (0, 1 o 2): sara' il confronto con i
    vertici della mesh a scegliere quella giusta.
    """
    if pa is None or pb is None:
        return []
    ka, kb = pa.kind, pb.kind
    if (ka, kb) in ((AXIAL, PLANE), (SPHERE, PLANE), (SPHERE, AXIAL),
                    (TORUS, PLANE), (TORUS, AXIAL), (TORUS, SPHERE)):
        pa, pb = pb, pa
        ka, kb = pa.kind, pb.kind
    out: List[_Curve] = []
    try:
        # ---------- piano x piano -> retta ----------
        if ka == PLANE and kb == PLANE:
            d = np.cross(pa.axis, pb.axis)
            nd = np.linalg.norm(d)
            if nd < 1e-9:
                return []
            d /= nd
            A = np.vstack([pa.axis, pb.axis, d])
            rhs = np.array([pa.axis @ pa.center, pb.axis @ pb.center, 0.0])
            p = np.linalg.solve(A, rhs)
            out.append(CLine(p, d))

        # ---------- piano x sfera -> cerchio ----------
        elif ka == PLANE and kb == SPHERE:
            h = float((pb.center - pa.center) @ pa.axis)
            if abs(h) > pb.r0 + tol:
                return []
            rr = math.sqrt(max(pb.r0 ** 2 - h ** 2, 0.0))
            if rr < 10 * tol:
                return []
            out.append(_circle(pb.center - h * pa.axis, pa.axis, rr))

        # ---------- piano x cilindro/cono ----------
        elif ka == PLANE and kb == AXIAL:
            # ⚠️ Nessun ramo esclusivo: si generano TUTTE le interpretazioni
            # plausibili e sara' il residuo sui vertici a scegliere. Un asse
            # fittato e' parallelo al piano a meno di 1e-6 rad, mai a meno di
            # 1e-9: un test rigido sceglierebbe l'ellisse con semiasse di 4e6 mm
            # al posto della retta di tangenza.
            n, a = pa.axis, pb.axis
            cph = float(a @ n)
            if abs(cph) > 1.0 - 1e-6:                       # piano ORTOGONALE
                tstar = float((pa.center - pb.center) @ n) / cph
                r = pb.r0 + pb.slope * tstar
                if r > 10 * tol:
                    out.append(_circle(pb.center + tstar * a, a, r))
            if abs(pb.slope) < 1e-9:
                if abs(cph) < 1e-2:                         # piano ~PARALLELO
                    h = float((pb.center - pa.center) @ n)
                    foot = pb.center - h * n
                    w = np.cross(n, a)
                    nw = np.linalg.norm(w)
                    if nw > 1e-12 and abs(h) <= pb.r0 * 1.02 + tol:
                        w /= nw
                        s = math.sqrt(max(pb.r0 ** 2 - h ** 2, 0.0))
                        out.append(CLine(foot, a))          # TANGENZA
                        if s > max(tol, 1e-9):
                            out.append(CLine(foot + s * w, a))
                            out.append(CLine(foot - s * w, a))
                if 1e-9 < abs(cph) < 1.0 - 1e-9:            # piano OBLIQUO
                    tstar = float((pa.center - pb.center) @ n) / cph
                    c = pb.center + tstar * a
                    umaj = a - cph * n
                    nu = np.linalg.norm(umaj)
                    if nu > 1e-12:
                        umaj /= nu
                        el = _ellipse(c, n, umaj, pb.r0 / abs(cph), pb.r0)
                        if el is not None:
                            out.append(el)

        # ---------- piano x toro ----------
        elif ka == PLANE and kb == TORUS:
            n, a = pa.axis, pb.axis
            cph = float(a @ n)
            if abs(cph) > 1.0 - 1e-6:                # piano ORTOGONALE all'asse
                t = float((pa.center - pb.center) @ n) / cph
                if abs(t) <= pb.r1 + tol:
                    s = math.sqrt(max(pb.r1 ** 2 - t ** 2, 0.0))
                    ctr = pb.center + t * a
                    for rr in (pb.r0 + s, pb.r0 - s):
                        if rr > 10 * tol:
                            out.append(_circle(ctr, a, rr))
            elif abs(cph) < 1e-2:                    # piano MERIDIANO
                h = float((pb.center - pa.center) @ n)
                if abs(h) <= max(tol, 1e-6):
                    w = np.cross(n, a)
                    nw = np.linalg.norm(w)
                    if nw > 1e-12:
                        w /= nw
                        for sg in (+1.0, -1.0):
                            out.append(_circle(pb.center + sg * pb.r0 * w,
                                               n, pb.r1))

        # ---------- cilindro x toro ----------
        elif ka == AXIAL and kb == TORUS:
            if abs(pa.slope) > 1e-9:
                return []
            d = pb.center - pa.center
            t0 = float(d @ pa.axis)
            off = d - t0 * pa.axis
            # ⚠️ RACCORDO DRITTO CHE CONTINUA NEL RACCORDO D'ANGOLO: assi
            # ORTOGONALI, stesso raggio del tubo, asse del cilindro a distanza
            # R dal centro del toro. Le due superfici sono tangenti lungo il
            # meridiano del toro: un cerchio di raggio r nel piano ortogonale
            # all'asse del cilindro passante per il centro del toro. Senza
            # questo caso il confine restava la scaletta della mesh.
            if abs(float(pa.axis @ pb.axis)) < 1e-3:
                rt = pb.r1
                if abs(pa.r0 - rt) <= max(tol, 1e-3 * pa.r0) and                         abs(float(np.linalg.norm(off)) - pb.r0) <= max(10.0 * tol, 1e-3 * pb.r0):
                    cj = pa.center + t0 * pa.axis                  # punto dell'asse del cilindro piu' vicino a C
                    out.append(_circle(cj, pa.axis, pa.r0))
                return out
            if float(np.linalg.norm(off)) > max(tol, 1e-6):
                return []
            if abs(float(pa.axis @ pb.axis)) < 1.0 - 1e-6:
                return []
            dr = pa.r0 - pb.r0
            if abs(dr) > pb.r1 + tol:
                return []
            s = math.sqrt(max(pb.r1 ** 2 - dr ** 2, 0.0))
            for sg in ((0.0,) if s <= max(tol, 1e-9) else (+1.0, -1.0)):
                out.append(_circle(pa.center + (t0 + sg * s) * pa.axis,
                                   pa.axis, pa.r0))

        # ---------- toro x toro (coassiali) -> cerchi ----------
        elif ka == TORUS and kb == TORUS:
            if abs(float(pa.axis @ pb.axis)) < 1.0 - 1e-6:
                return []
            d = pb.center - pa.center
            t0 = float(d @ pa.axis)
            if float(np.linalg.norm(d - t0 * pa.axis)) > max(tol, 1e-6):
                return []
            return []          # quartica: se ne occupa il fit sui punti

        # ---------- cilindro x sfera (coassiali) -> cerchi ----------
        elif ka == AXIAL and kb == SPHERE:
            if abs(pa.slope) > 1e-9:
                return []
            d = pb.center - pa.center
            t0 = float(d @ pa.axis)
            off = float(np.linalg.norm(d - t0 * pa.axis))
            if off > max(tol, 1e-6):
                return []
            # ⚠️ sqrt(rs^2 - rc^2) e' inutilizzabile come test di tangenza:
            # con rs e rc uguali a meno di 1e-6 mm il radicando e' -8e-6 e il
            # ramo tangente non scatta mai. Il confronto va fatto sui RAGGI.
            dr = pb.r0 - pa.r0
            if abs(dr) <= max(tol, 1e-3 * pa.r0):
                out.append(_circle(pa.center + t0 * pa.axis, pa.axis, pa.r0))
            if dr > 0.0:
                h = math.sqrt(max(pb.r0 ** 2 - pa.r0 ** 2, 0.0))
                if h > max(tol, 1e-9):
                    out.append(_circle(pa.center + (t0 + h) * pa.axis, pa.axis, pa.r0))
                    out.append(_circle(pa.center + (t0 - h) * pa.axis, pa.axis, pa.r0))

        # ---------- sfera x sfera -> cerchio ----------
        elif ka == SPHERE and kb == SPHERE:
            d = pb.center - pa.center
            dd = float(np.linalg.norm(d))
            if dd < 1e-9:
                return []
            x = (dd ** 2 + pa.r0 ** 2 - pb.r0 ** 2) / (2.0 * dd)
            rr2 = pa.r0 ** 2 - x ** 2
            if rr2 < -(tol ** 2):
                return []
            out.append(_circle(pa.center + x * (d / dd), d / dd,
                               math.sqrt(max(rr2, 0.0))))

        # ---------- cilindro x cilindro (assi paralleli) -> rette ----------
        elif ka == AXIAL and kb == AXIAL:
            if abs(pa.slope) > 1e-9 or abs(pb.slope) > 1e-9:
                return []
            if abs(float(pa.axis @ pb.axis)) < 1.0 - 1e-9:
                # ⚠️ SPIGOLO VIVO FRA DUE RACCORDI. Dove due raccordi dello
                # stesso raggio si incontrano senza sfera d'angolo, i due
                # cilindri hanno assi INCIDENTI: l'intersezione non e' una
                # quartica ma DUE ELLISSI piane (Steinmetz). Senza questo caso
                # quello spigolo resta poligonale e la faccia non chiude.
                if abs(pa.r0 - pb.r0) > max(tol, 1e-3 * pa.r0):
                    return []
                a1 = pa.axis
                a2 = pb.axis * (1.0 if float(pa.axis @ pb.axis) > 0 else -1.0)
                w0 = pa.center - pb.center
                b_ = float(a1 @ a2)
                den = 1.0 - b_ * b_
                if abs(den) < 1e-12:
                    return []
                dd_ = float(a1 @ w0); ee_ = float(a2 @ w0)
                s_ = (b_ * ee_ - dd_) / den
                t_ = (ee_ - b_ * dd_) / den
                Q1 = pa.center + s_ * a1
                Q2 = pb.center + t_ * a2
                if float(np.linalg.norm(Q1 - Q2)) > max(10.0 * tol, 1e-6 * pa.r0):
                    return []
                Q = 0.5 * (Q1 + Q2)
                w = np.cross(a1, a2)
                nw = np.linalg.norm(w)
                if nw < 1e-12:
                    return []
                w /= nw
                th = math.acos(max(-1.0, min(1.0, b_)))
                r = 0.5 * (pa.r0 + pb.r0)
                for sgn in (-1.0, +1.0):
                    nrm = a1 + sgn * a2
                    maj = a1 - sgn * a2
                    if np.linalg.norm(nrm) < 1e-9 or np.linalg.norm(maj) < 1e-9:
                        continue
                    nrm = nrm / np.linalg.norm(nrm)
                    maj = maj / np.linalg.norm(maj)
                    half = math.cos(th / 2.0) if sgn > 0 else math.sin(th / 2.0)
                    if abs(half) < 1e-9:
                        continue
                    el = _ellipse(Q, nrm, maj, r / abs(half), r)
                    if el is not None:
                        out.append(el)
                return out
            a = pa.axis
            d = pb.center - pa.center
            perp = d - float(d @ a) * a
            dd = float(np.linalg.norm(perp))
            if dd < 1e-9:
                return []
            ex = perp / dd
            ey = np.cross(a, ex)
            x = (dd ** 2 + pa.r0 ** 2 - pb.r0 ** 2) / (2.0 * dd)
            y2 = pa.r0 ** 2 - x ** 2
            if y2 < -(tol ** 2):
                return []
            y = math.sqrt(max(y2, 0.0))
            base = pa.center + x * ex
            if y <= max(tol, 1e-9):
                out.append(CLine(base, a))
            else:
                out.append(CLine(base + y * ey, a))
                out.append(CLine(base - y * ey, a))
    except Exception as e:
        Log.debug(f"intersezione {ka}x{kb} fallita: {e}")
        return []
    return out


# --- 5C.7  fit di ripiego (quando l'intersezione analitica non e' disponibile)

def fit_curve(P: np.ndarray, tol: float, arc_tol: Optional[float] = None) -> Optional[_Curve]:
    """Retta o cerchio per i vertici (tol sui vertici, arc_tol sull'arco fra i vertici)."""
    if arc_tol is None:
        arc_tol = tol
    if len(P) < 2:
        return None
    c = P.mean(axis=0)
    Q = P - c
    _, S, Vt = np.linalg.svd(Q, full_matrices=False)
    line = CLine(c, Vt[0])
    if float(np.abs(line.dist(P)).max()) <= tol:
        return line
    if len(P) < 4:
        return None
    n = Vt[-1]
    if float(np.abs(Q @ n).max()) > tol:
        return None
    u, v = ortho_frame(n)
    try:
        cx, cy, r = taubin_circle(Q @ u, Q @ v)
    except Exception:
        return None
    if not (np.isfinite(cx) and np.isfinite(cy) and np.isfinite(r)) or r <= 1e-9:
        return None
    circ = CCircle(c + cx * u + cy * v, n, u, np.cross(n, u), float(r))
    if (float(np.abs(circ.dist(P)).max()) <= tol
            and arc_deviation(circ, P) <= arc_tol):
        return circ
    return None


def arc_deviation(cv: _Curve, P: np.ndarray, dens: int = 6) -> float:
    """
    Scarto dell'ARCO PERCORSO dalla spezzata dei vertici, non solo dei vertici.

    ⚠️ E' IL CONTROLLO CHE MANCAVA, ED E' LA CAUSA DELLE CURVE CHE SPORGONO.
    Giudicare una curva solo NEI VERTICI e' come giudicare un ponte guardando i
    piloni: un'ellisse con semiasse di 889 mm su un pezzo da 49 mm puo' passare
    esattamente per tutti i vertici di una catena corta e poi, FRA un vertice e
    l'altro, allontanarsi di millimetri fuori dal pezzo. Qui la curva viene
    campionata fitta lungo tutto l'arco e confrontata con la spezzata dei
    vertici: se se ne stacca, e' bocciata.
    """
    P = np.atleast_2d(P)
    if len(P) < 2:
        return 0.0
    try:
        t = np.asarray(cv.param(P), dtype=float)
        if cv.period:
            t = np.unwrap(t)
        ts = [np.linspace(t[k], t[k + 1], dens, endpoint=False)
              for k in range(len(t) - 1)]
        ts.append(np.array([t[-1]]))
        S_ = cv.point(np.concatenate(ts))
    except Exception:
        return math.inf
    A = P[:-1]
    B = P[1:]
    AB = B - A
    L2 = np.maximum(np.einsum("ij,ij->i", AB, AB), 1e-30)
    worst = 0.0
    for s in S_:
        u = np.clip(np.einsum("ij,ij->i", s - A, AB) / L2, 0.0, 1.0)
        d = float(np.linalg.norm(A + u[:, None] * AB - s, axis=1).min())
        if d > worst:
            worst = d
    return worst


def choose_curve(cands: List[_Curve], P: np.ndarray, tol: float,
                 scale: float = 0.0, arc_tol: Optional[float] = None) -> Optional[_Curve]:
    """
    tol     : scarto max dei VERTICI dalla curva.
    arc_tol : scarto max dell'ARCO dalla spezzata dei vertici (deve ammettere la
              freccia delle corde della mesh, altrimenti nessun cerchio passa).
    """
    if arc_tol is None:
        arc_tol = tol
    best, bs = None, math.inf
    for cv in cands:
        if scale > 0:
            big = getattr(cv, "a", None)
            if big is None:
                big = getattr(cv, "r", None)
            if big is not None and float(big) > 20.0 * scale:
                continue          # primitiva enorme: artefatto del fit
        try:
            s = float(np.abs(cv.dist(P)).max())
        except Exception:
            continue
        if s > tol:
            continue
        s2 = arc_deviation(cv, P)
        if s2 > arc_tol:
            continue
        sc = max(s, s2)
        if sc < bs:
            best, bs = cv, sc
    return best

# --- 5C.8  nodi: posizione esatta come intersezione delle curve incidenti ----



# =============================================================================
# 7. INDICE TOPOLOGICO della shape corrente
# =============================================================================


class Topo:
    """
    Facce, spigoli, vertici della shape con indici interi stabili finche' la
    shape non cambia. Si ricostruisce (costa ~0.1 s su 5.000 facce) dopo ogni
    sostituzione accettata.
    """

    __slots__ = ("shape", "faces", "fmap", "nF", "verts", "norms", "areas",
                 "cents", "nverts", "edges", "emap", "nE", "e_faces", "f_edges",
                 "e_verts", "vmap", "nV", "vpos", "adj", "planar")

    def __init__(self, shape, prev: Optional["Topo"] = None):
        self.shape = shape
        fmap = TopTools_IndexedMapOfShape()
        te_MapShapes(shape, TopAbs_FACE, fmap)
        self.fmap = fmap
        self.nF = _size(fmap)
        self.faces = [td_Face(fmap.FindKey(i)) for i in range(1, self.nF + 1)]

        vmap = TopTools_IndexedMapOfShape()
        te_MapShapes(shape, TopAbs_VERTEX, vmap)
        self.vmap = vmap
        self.nV = _size(vmap)
        self.vpos = np.array([vpos(vmap.FindKey(j)) for j in range(1, self.nV + 1)],
                             dtype=float).reshape(-1, 3)

        self.verts, self.cents, self.norms, self.areas, self.nverts, self.planar = \
            {}, {}, {}, {}, {}, {}
        for i, f in enumerate(self.faces):
            # ⚠️ le facce non toccate dall'ultima sostituzione sono le stesse
            # (IsSame): i loro dati si copiano dall'indice precedente
            if prev is not None:
                k = prev.fmap.FindIndex(f) - 1
                if k >= 0 and prev.faces[k].Orientation() == f.Orientation():
                    self.verts[i] = prev.verts[k]
                    self.nverts[i] = prev.nverts[k]
                    self.cents[i] = prev.cents[k]
                    self.areas[i] = prev.areas[k]
                    self.planar[i] = prev.planar[k]
                    self.norms[i] = prev.norms[k]
                    continue
            m = TopTools_IndexedMapOfShape()
            te_MapShapes(f, TopAbs_VERTEX, m)
            idx = [vmap.FindIndex(m.FindKey(k)) - 1 for k in range(1, _size(m) + 1)]
            idx = [j for j in idx if j >= 0]
            V = self.vpos[idx] if idx else np.zeros((0, 3))
            self.verts[i] = V
            self.nverts[i] = len(idx)
            self.cents[i] = V.mean(axis=0) if len(V) else np.zeros(3)
            self.areas[i] = face_area(f)
            n = face_plane_normal(f)
            self.planar[i] = n is not None
            self.norms[i] = n if n is not None else np.zeros(3)

        ef, _, e_faces = face_edges_map(shape)
        self.emap = ef
        self.nE = _size(ef)
        # ⚠️ la mappa restituisce ogni spigolo con l'orientamento del PRIMO uso
        # incontrato: qui li si normalizza a FORWARD, cosi' "fwd" vuol dire
        # sempre "da FirstVertex a LastVertex" e Reversed() fa quel che dice.
        self.edges = [td_Edge(ef.FindKey(k).Oriented(TopAbs_FORWARD))
                      for k in range(1, self.nE + 1)]
        self.e_faces = [sorted(fs) for fs in e_faces]
        self.f_edges = [[] for _ in range(self.nF)]
        for k, fs in enumerate(self.e_faces):
            for i in fs:
                self.f_edges[i].append(k)
        self.e_verts = []
        for e in self.edges:
            a = vmap.FindIndex(te_FirstVertex(e)) - 1
            b = vmap.FindIndex(te_LastVertex(e)) - 1
            self.e_verts.append((a, b))
        self.adj = [set() for _ in range(self.nF)]
        for fs in self.e_faces:
            for a in range(len(fs)):
                for b in range(a + 1, len(fs)):
                    self.adj[fs[a]].add(fs[b])
                    self.adj[fs[b]].add(fs[a])

    def face_index(self, face) -> int:
        return self.fmap.FindIndex(face) - 1

    def edge_index(self, edge) -> int:
        return self.emap.FindIndex(edge) - 1

    def vertex_index(self, v) -> int:
        return self.vmap.FindIndex(v) - 1


def plane_prim_of_face(topo: Topo, i: int) -> Optional["Prim"]:
    if not topo.planar[i]:
        return None
    return Prim(PLANE, face_plane_point(topo.faces[i]), topo.norms[i].copy())


# =============================================================================
# 8. SEGMENTAZIONE: regioni di faccette che stanno su UNA primitiva curva
# =============================================================================
#
# Crescita da seme guidata dal modello (come nel motore precedente), ma con un
# ciclo "raffina -> ricresci" e una purga finale alla tolleranza STRETTA: una
# regione entra in gioco solo se OGNI suo vertice sta sulla superficie a meno
# del rumore della mesh. Le facce che non reggono restano libere, e libere
# restano: non si forza niente.


@dataclass
class Region:
    prim: "Prim"
    faces: List[int]
    rms: float = 0.0
    max_res: float = 0.0
    sag: float = 0.0            # scarto max faccetta-superficie (freccia delle corde)
    closed_u: bool = False      # chiusa a 360 gradi attorno all'asse
    concave: bool = False       # materiale all'ESTERNO della superficie (foro)
    t_lo: float = 0.0
    t_hi: float = 0.0
    coverage: float = 0.0       # frazione dell'angolo giro coperta
    status: str = ""            # esito della conversione
    note: str = ""

    def label(self) -> str:
        p = self.prim
        if p.kind == SPHERE:
            dim = f"ø{2*p.r0:.4f}"
        elif p.kind == TORUS:
            dim = f"R{p.r0:.4f} r{p.r1:.4f}"
        elif abs(p.slope) < 1e-9:
            dim = f"ø{2*p.r0:.4f}"
        else:
            dim = f"ø{2*(p.r0+p.slope*self.t_lo):.4f}-{2*(p.r0+p.slope*self.t_hi):.4f}"
        kind = ("CONCAVO" if self.concave else "CONVESSO")
        cl = "360°" if self.closed_u else f"{self.coverage*360:.0f}°"
        return f"{p.label():<6} {dim:<22} {kind:<8} {cl:>5} {len(self.faces):>5} facce"


def _ring(seed: int, adj, taken, rings: int = 2, cap: int = 80,
          norms=None, cos_smooth: float = None) -> List[int]:
    """Anelli completi di adiacenza attorno al seme, solo attraverso spigoli lisci."""
    out, seen, frontier = [seed], {seed}, [seed]
    for _ in range(max(1, rings)):
        nxt = []
        for f in frontier:
            for nb in adj[f]:
                if nb in seen or taken[nb]:
                    continue
                if norms is not None and cos_smooth is not None:
                    na, nb_ = norms[f], norms[nb]
                    if np.dot(na, na) > 0.5 and np.dot(nb_, nb_) > 0.5:
                        if abs(float(na @ nb_)) < cos_smooth:
                            continue
                seen.add(nb)
                out.append(nb)
                nxt.append(nb)
        frontier = nxt
        if not frontier or len(out) >= cap:
            break
    return out


def grow_region(seed_faces, prim, adj, taken, verts, norms, areas,
                tol: float, cos_ang: float, max_faces: int = 200000,
                refit: bool = True):
    region = set(seed_faces)
    frontier = list(seed_faces)
    next_refit = max(12, 2 * len(seed_faces)) if refit else 10 ** 9
    while frontier:
        f = frontier.pop()
        for nb in adj[f]:
            if nb in region or taken[nb]:
                continue
            V = verts[nb]
            if V.size == 0:
                continue
            if float(np.abs(prim.dist(V)).max()) > tol:
                continue
            nrm = norms[nb]
            if np.dot(nrm, nrm) < 0.5:
                continue
            pn = prim.normal_at(V.mean(axis=0)[None, :])[0]
            if abs(float(pn @ nrm)) < cos_ang:
                continue
            region.add(nb)
            frontier.append(nb)
            if len(region) >= max_faces:
                frontier = []
                break
        if len(region) >= next_refit:
            newp = _refit(region, verts, norms, areas, prim.kind)
            if newp is not None:
                prim = newp
            next_refit = int(next_refit * 2)
    return sorted(region), prim


def largest_component(faces_idx, adj) -> List[int]:
    fs = set(faces_idx)
    best: List[int] = []
    while fs:
        s = fs.pop()
        comp, stack = {s}, [s]
        while stack:
            f = stack.pop()
            for nb in adj[f]:
                if nb in fs:
                    fs.discard(nb)
                    comp.add(nb)
                    stack.append(nb)
        if len(comp) > len(best):
            best = sorted(comp)
    return best


def region_sag(prim: "Prim", topo: Topo, faces: List[int]) -> float:
    """Freccia: distanza max fra i punti INTERNI delle faccette e la superficie."""
    pts = []
    for i in faces:
        V = topo.verts[i]
        if len(V) < 3:
            continue
        pts.append(V.mean(axis=0))
        pts.append(0.5 * (V[0] + V[1]))
        pts.append(0.5 * (V[1] + V[2]))
        pts.append(0.5 * (V[0] + V[2]))
        if len(V) > 3:
            pts.append(0.5 * (V[-1] + V[0]))
    if not pts:
        return 0.0
    return float(np.abs(prim.dist(np.array(pts))).max())


def describe_region(prim: "Prim", topo: Topo, faces: List[int]) -> Region:
    P = np.vstack([topo.verts[i] for i in faces])
    d = prim.dist(P)
    R = Region(prim=prim, faces=list(faces),
               rms=float(np.sqrt(np.mean(d ** 2))), max_res=float(np.abs(d).max()))
    R.sag = region_sag(prim, topo, faces)
    C = np.array([topo.cents[i] for i in faces])
    N = np.array([topo.norms[i] for i in faces])
    W = np.array([max(topo.areas[i], 1e-12) for i in faces])
    nat = prim.normal_at(C)
    R.concave = float(np.sum(W * np.einsum("ij,ij->i", nat, N))) < 0.0
    if prim.kind in (AXIAL, TORUS):
        dd = P - prim.center
        t = dd @ prim.axis
        R.t_lo, R.t_hi = float(t.min()), float(t.max())
        u, v = ortho_frame(prim.axis)
        ang = np.sort(np.unique(np.round(np.arctan2(dd @ v, dd @ u), 6)))
        if len(ang) >= 3:
            gaps = np.diff(np.concatenate([ang, [ang[0] + 2 * math.pi]]))
            med = float(np.median(gaps))
            gmax = float(gaps.max())
            R.coverage = float(min(1.0, (2 * math.pi - gmax) / (2 * math.pi)))
            R.closed_u = gmax <= max(4.0 * med, math.radians(2.0)) and R.coverage > 0.9
        else:
            R.coverage = 0.0
    elif prim.kind == SPHERE:
        dd = C - prim.center
        dd = dd / np.maximum(np.linalg.norm(dd, axis=1), 1e-12)[:, None]
        m = (dd * W[:, None]).sum(axis=0) / W.sum()
        R.coverage = float(1.0 - np.linalg.norm(m))   # 0 = calotta piccola, 1 = sfera intera
        R.closed_u = False
    return R


def prims_equal(pa: "Prim", pb: "Prim", tol_len: float) -> bool:
    return prims_same(pa, pb, tol_len, math.cos(math.radians(0.05)))




_KIND_RANK = {AXIAL: 0, SPHERE: 2, TORUS: 3}


def _rank(p: "Prim") -> int:
    if p.kind == AXIAL:
        return 0 if abs(p.slope) < 1e-9 else 1
    return _KIND_RANK.get(p.kind, 9)


def prim_radius(p: "Prim") -> float:
    if p.kind == PLANE:
        return 0.0
    if p.kind == TORUS:
        return float(p.r0 + p.r1)
    return float(abs(p.r0))


class Segmenter:
    """
    Regioni di faccette che stanno ESATTAMENTE su cilindri/coni/sfere/tori.
    only_cyl: cerca solo cilindri (Fase B).
    """

    # ⚠️ seed_smooth = 20 gradi: l'intorno del seme attraversa solo spigoli
    # quasi lisci. Con 50 gradi entravano le faccette degli SMUSSI a 45 gradi
    # accanto a una striscia di cilindro, il fit del seme era spazzatura e le
    # estremita' arrotondate del pezzo non venivano mai riconosciute.
    def __init__(self, topo, tol_fit: float, tol_grow: float, diag: float,
                 min_faces: int = 4, grow_angle: float = 35.0, seed_smooth: float = 20.0,
                 allow_sphere: bool = True, allow_cone: bool = True,
                 allow_torus: bool = True, only_cyl: bool = False, threads: int = 1):
        self.topo = topo
        self.threads = max(1, int(threads))
        # parametri esatti del costruttore: servono per ricostruire lo stesso
        # Segmenter dentro i processi di lavoro
        self._kw = dict(tol_fit=tol_fit, tol_grow=tol_grow, diag=diag, min_faces=min_faces,
                        grow_angle=grow_angle, seed_smooth=seed_smooth,
                        allow_sphere=allow_sphere, allow_cone=allow_cone,
                        allow_torus=allow_torus, only_cyl=only_cyl, threads=1)
        self.tol_fit, self.tol_grow, self.diag = tol_fit, tol_grow, diag
        self.min_faces = min_faces
        self.cos_ang = math.cos(math.radians(grow_angle))
        self.cos_seed = math.cos(math.radians(seed_smooth))
        self.allow_sphere, self.allow_cone, self.allow_torus = allow_sphere, allow_cone, allow_torus
        self.only_cyl = only_cyl
        self.max_radius = 1.5 * diag
        self.taken = [False] * topo.nF

    # --- criteri -----------------------------------------------------------------
    def within(self, p, i) -> bool:
        V = self.topo.verts[i]
        return bool(V.size) and float(np.abs(p.dist(V)).max()) <= self.tol_fit

    def kind_ok(self, p) -> bool:
        if p is None or p.kind == PLANE:
            return False
        if self.only_cyl:
            return p.kind == AXIAL and abs(p.slope) < 1e-9
        return True

    def is_flat(self, reg) -> bool:
        """
        Regione indistinguibile da un piano: non e' una lavorazione curva.
        Il segnale di curvatura (scarto dal piano) deve superare di netto la
        tolleranza, altrimenti e' rumore che un cilindro enorme "spiega" per caso.
        """
        P = np.vstack([self.topo.verts[i] for i in reg])
        pl = fit_plane(P)
        return pl is not None and float(np.abs(pl.dist(P)).max()) <= 3.0 * self.tol_fit

    # --- scelta del tipo -----------------------------------------------------------
    def choose_prim(self, faces: List[int], max_ndev: float = 15.0) -> Optional["Prim"]:
        """
        Rimette in discussione il TIPO di primitiva su una regione cresciuta,
        provando PRIMA LA PIU' SEMPLICE e fermandosi alla prima che regge
        (cilindro < cono < sfera < toro).

        ⚠️ Una parete di foro tassellata a strisce ha i vertici SOLO sui due
        cerchi di bordo: quei punti stanno esattamente anche su una sfera. Il
        residuo non distingue, le NORMALI si': la sfera le vorrebbe inclinate
        di decine di gradi. Per questo ogni candidata passa anche il controllo
        delle normali.
        """
        topo = self.topo
        idx = list(faces)
        if len(idx) < 3:
            return None
        P = np.vstack([topo.verts[i] for i in idx])
        Nrep = np.vstack([np.tile(topo.norms[i], (len(topo.verts[i]), 1)) for i in idx])
        fallback = [None]

        def judge(p):
            if p is None or prim_radius(p) > self.max_radius:
                return False
            try:
                mx = float(np.abs(p.dist(P)).max())
                nd = normal_deviation(p, P, Nrep)
            except Exception:
                return False
            if not (np.isfinite(mx) and np.isfinite(nd)) or nd > max_ndev:
                return False
            if mx <= self.tol_fit:
                return True
            if fallback[0] is None or mx < fallback[0][0]:
                fallback[0] = (mx, p)
            return False

        ax = refit_exact(idx, topo.verts, topo.norms, topo.areas, AXIAL)
        if ax is not None:
            cyl = ax
            if abs(ax.slope) > 1e-9:
                cyl = lm_refine(Prim(AXIAL, ax.center.copy(), ax.axis.copy(), ax.r0, 0.0), P)
            if judge(cyl):
                return cyl
            if self.allow_cone and not self.only_cyl and abs(ax.slope) > 1e-9 and judge(ax):
                return ax
        if not self.only_cyl:
            if self.allow_sphere:
                sp = refit_exact(idx, topo.verts, topo.norms, topo.areas, SPHERE)
                if judge(sp):
                    return sp
            if self.allow_torus:
                tr = refit_exact(idx, topo.verts, topo.norms, topo.areas, TORUS)
                if judge(tr):
                    return tr
        return fallback[0][1] if fallback[0] else None

    # --- semi ------------------------------------------------------------------------
    def seed_candidates(self, seed: List[int]):
        """
        Primitive candidate dall'intorno del seme, ROBUSTE ai vicini estranei.

        ⚠️ L'intorno attraversa gli spigoli lisci, quindi su un raccordo
        (tangente per definizione ai suoi vicini) raccoglie anche faccette di
        ALTRE superfici e magari la faccia piana grande accanto: il fit su quel
        miscuglio e' spazzatura e il seme muore. Qui si tolgono le facce
        grandi, si fitta, si tengono le faccette entro la tolleranza di
        crescita e si rifitta sulle sole "inlier".
        """
        topo = self.topo
        med = float(np.median([topo.areas[i] for i in seed]))
        core = [i for i in seed if topo.nverts[i] <= 8 and topo.areas[i] <= 12.0 * med]
        if len(core) < 4:
            return []
        vs = [topo.verts[i] for i in core]
        P = np.vstack(vs)
        Nrep = np.vstack([np.tile(topo.norms[i], (len(v), 1)) for i, v in zip(core, vs)])
        N = np.array([topo.norms[i] for i in core])
        W = np.array([max(topo.areas[i], 1e-9) for i in core])
        pl = fit_plane(P)
        if pl is not None and float(np.abs(pl.dist(P)).max()) <= 3.0 * self.tol_fit:
            return []
        out = []
        raw = rank_primitives(P, Nrep, N, W, self.allow_sphere and not self.only_cyl,
                              self.allow_cone and not self.only_cyl, max_ndev=60.0,
                              allow_torus=self.allow_torus and not self.only_cyl)
        raw = [c for c in raw if c.kind != PLANE]
        # ⚠️ i fit algebrici su 4-10 faccette sbagliano anche del 40% sul raggio:
        # senza un raffinamento LM la sfera vera non entra mai in tolleranza e
        # il seme muore. Qui ogni candidata (piu' una sfera e un cilindro
        # tentati comunque) viene raffinata sulle sole inlier, tre volte.
        extra = []
        if not self.only_cyl and self.allow_sphere and not any(c.kind == SPHERE for c in raw):
            sp0 = fit_sphere(P)
            if sp0 is not None:
                extra.append(sp0)
        if not any(c.kind == AXIAL for c in raw):
            ax0 = fit_axial(P, N, W)
            if ax0 is not None:
                if self.only_cyl:
                    ax0.slope = 0.0
                extra.append(ax0)
        for c in raw + extra:
            if self.only_cyl:
                if c.kind != AXIAL:
                    continue
                c.slope = 0.0
            try:
                c = lm_refine(c, P, iters=12)
            except Exception:
                continue
            inl = core
            for _ in range(3):
                inl2 = [i for i in core
                        if float(np.abs(c.dist(topo.verts[i])).max()) <= self.tol_grow]
                if len(inl2) < 3:
                    c = None
                    break
                if set(inl2) == set(inl) and _ > 0:
                    break
                inl = inl2
                c2 = refit_exact(inl, topo.verts, topo.norms, topo.areas, c.kind)
                if c2 is None:
                    break
                if self.only_cyl:
                    c2.slope = 0.0
                c = c2
            if c is not None and prim_radius(c) <= self.max_radius:
                out.append((c, inl))
        out.sort(key=lambda t: (-len(t[1]), _rank(t[0])))
        return out[:3]

    # --- consolidamento ---------------------------------------------------------------
    def settle(self, reg, prim):
        """raffina -> ricresci -> purga stretta, finche' la regione e' stabile."""
        topo = self.topo
        reg = list(reg)
        p = prim
        last_n = 0
        if self.is_flat(reg):
            return None, "piatta"
        for _ in range(6):
            if len(reg) > 1.25 * last_n or last_n == 0:
                p2 = self.choose_prim(reg)
                last_n = len(reg)
            else:
                p2 = refit_exact(reg, topo.verts, topo.norms, topo.areas, p.kind)
                if p2 is not None and prim_radius(p2) > self.max_radius:
                    p2 = None
            if not self.kind_ok(p2) and len(reg) >= 6:
                # ⚠️ regione contaminata da faccette di superfici vicine (tipico
                # con la tolleranza di crescita sulle strisce lunghe): si tiene
                # la meta' che il fit del SEME spiega meglio e si riprova
                res = np.array([float(np.abs(p.dist(topo.verts[i])).max()) for i in reg])
                cut = max(self.tol_fit, float(np.median(res)))
                trimmed = largest_component([i for i, r_ in zip(reg, res) if r_ <= cut], topo.adj)
                if len(trimmed) >= 4 and len(trimmed) < len(reg):
                    reg = trimmed
                    p2 = self.choose_prim(reg)
            if not self.kind_ok(p2):
                return None, "nessuna primitiva regge"
            p = p2
            # ⚠️ REGIONE CONTAMINATA: la crescita dal seme sbagliato ha preso
            # anche faccette di superfici vicine e il fit e' un compromesso.
            # Si tiene la meta' migliore, si rifitta e si riparte da li'.
            res = np.array([float(np.abs(p.dist(topo.verts[i])).max()) for i in reg])
            if res.max() > self.tol_fit and len(reg) >= 6:
                cut = max(self.tol_fit, float(np.median(res)))
                trimmed = largest_component([i for i, r_ in zip(reg, res) if r_ <= cut], topo.adj)
                if len(trimmed) >= 3:
                    p3 = refit_exact(trimmed, topo.verts, topo.norms, topo.areas, p.kind)
                    if p3 is not None and prim_radius(p3) <= self.max_radius:
                        p = p3
                        reg = trimmed
            reg2, _ = grow_region(reg, p, topo.adj, self.taken, topo.verts, topo.norms,
                                  topo.areas, self.tol_grow, self.cos_ang, refit=False)
            keep = largest_component([i for i in reg2 if self.within(p, i)], topo.adj)
            if len(keep) < self.min_faces:
                return None, f"troppo piccola ({len(keep)} facce entro tolleranza)"
            if set(keep) == set(reg):
                break
            reg = keep
        p = refit_exact(reg, topo.verts, topo.norms, topo.areas, p.kind)
        if not self.kind_ok(p) or prim_radius(p) > self.max_radius:
            return None, "fit finale non valido"
        if abs(p.slope) < 1e-9:
            p.slope = 0.0
        if not all(self.within(p, i) for i in reg):
            keep = largest_component([i for i in reg if self.within(p, i)], topo.adj)
            if len(keep) < self.min_faces:
                return None, "troppo piccola dopo il fit finale"
            reg = keep
        if self.is_flat(reg):
            return None, "piatta"
        return reg, p

    def try_seed(self, s: int):
        """(prim, facce) dalla faccia seme s, oppure (None, motivo)."""
        topo = self.topo
        # ⚠️ INTORNO ADATTIVO. Su una mesh finissima (faccette da 0.03 mm su un
        # raggio di 5 mm) due anelli di vicini sono piatti entro tolleranza:
        # la curvatura non si vede e il seme muore ("nessuna candidata"). Si
        # allarga l'intorno finche' la curvatura emerge o si esaurisce.
        cands = []
        last_n = 0
        for rings, cap in ((1, 40), (2, 80), (4, 250)):
            seed = _ring(s, topo.adj, self.taken, rings, cap=cap,
                         norms=topo.norms, cos_smooth=self.cos_seed)
            seed = [i for i in seed if topo.planar[i] and topo.verts[i].size]
            if len(seed) < 4 or len(seed) == last_n:
                if len(seed) == last_n:
                    break
                continue
            last_n = len(seed)
            cands += self.seed_candidates(seed)
            if cands:
                break
        if not cands:
            return None, "nessuna candidata sul seme"
        why = ""
        for prim, inl in cands:
            reg, p2 = grow_region(inl, prim, topo.adj, self.taken, topo.verts,
                                  topo.norms, topo.areas, self.tol_grow, self.cos_ang)
            if len(reg) < self.min_faces:
                why = "crescita insufficiente"
                continue
            keep, pk = self.settle(reg, p2)
            if keep is None:
                why = pk
                continue
            return (pk, keep), ""
        return None, why

    # --- ciclo principale --------------------------------------------------------------
    # --- ciclo dei semi, in sequenza ---------------------------------------------
    def _seed_loop(self, order, tries) -> Tuple[list, int]:
        topo = self.topo
        found: List[Tuple["Prim", List[int]]] = []
        tried = 0
        for s in order:
            if self.taken[s] or not topo.planar[s] or tries[s] >= 2:
                continue
            tried += 1
            res, why = self.try_seed(s)
            if res is None:
                tries[s] += 2
                continue
            pk, keep = res
            for i in keep:
                self.taken[i] = True
            found.append((pk, keep))
        return found, tried

    # --- ciclo dei semi, su piu' processi ------------------------------------------
    def _seed_loop_parallel(self, order, tries, pool) -> Tuple[list, int]:
        """
        Stessa ricerca, ma i semi sono divisi fra i processi: ognuno fa il suo
        goloso sulla propria fetta (marcandosi le faccette che prende, cosi'
        non ripassa venti volte sulla stessa regione) e poi il processo
        principale accetta le regioni UNA ALLA VOLTA, dalla piu' grande.

        ⚠️ La crescita dal seme legge quali faccette sono gia' prese: in
        parallelo ognuno lavora su una FOTOGRAFIA di quell'elenco, quindi due
        processi possono rivendicare le stesse faccette. Il controllo e' qui:
        una regione che ne tocca di gia' assegnate viene buttata e il suo seme
        torna in coda per il giro dopo, con l'elenco aggiornato. Alla fine i
        semi rimasti si fanno in sequenza, cosi' il risultato non dipende dal
        numero di processi piu' di quanto non dipenda dall'ordine dei semi.
        """
        import pickle
        import tempfile
        topo = self.topo
        found: List[Tuple["Prim", List[int]]] = []
        tried = 0
        fd, path = tempfile.mkstemp(suffix=".refit.pkl")
        with os.fdopen(fd, "wb") as fh:
            pickle.dump((TopoLite(topo), self._kw), fh, protocol=pickle.HIGHEST_PROTOCOL)
        key = os.path.basename(path)
        pending = [s for s in order if topo.planar[s]]
        try:
            for _round in range(4):
                pending = [s for s in pending if not self.taken[s] and tries[s] < 2]
                if len(pending) < 24:
                    break
                taken_b = bytes(1 if t else 0 for t in self.taken)
                # fette alternate: ogni processo vede semi sparsi su tutto il
                # pezzo, cosi' due processi raramente inseguono la stessa
                # regione, e il carico resta bilanciato
                n = min(_POOL_N or self.threads, max(1, len(pending) // 8))
                tasks = [(key, path, taken_b, pending[i::n]) for i in range(n)]
                try:
                    res = []
                    for part in pool.map(_worker_seeds, tasks):
                        res.extend(part)
                except Exception as ex:
                    Log.warn(f"processi di lavoro non disponibili ({type(ex).__name__}: {ex}): "
                             f"si prosegue su un solo core")
                    break
                # prima le regioni piu' grandi: in caso di sovrapposizione
                # vince quella che il ciclo sequenziale avrebbe trovato prima
                res.sort(key=lambda t: (t[1] is None,
                                        -sum(topo.areas[i] for i in t[2]) if t[1] is not None else 0.0))
                for s, prim, keep in res:
                    tried += 1
                    if prim is None:
                        tries[s] += 2
                        continue
                    if any(self.taken[i] for i in keep):
                        continue                              # conflitto: si riprova dopo
                    for i in keep:
                        self.taken[i] = True
                    found.append((prim, keep))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        f2, t2 = self._seed_loop([s for s in pending if not self.taken[s]], tries)
        return found + f2, tried + t2

    def run(self) -> List[Region]:
        topo = self.topo
        nF = topo.nF
        order = sorted(range(nF), key=lambda i: -topo.areas[i])
        tries = [0] * nF
        t_seed = time.perf_counter()
        pool = _seed_pool(self.threads, want=nF // 200) if nF >= 400 else None
        if pool is not None:
            found, tried = self._seed_loop_parallel(order, tries, pool)
        else:
            found, tried = self._seed_loop(order, tries)
        Log.debug(f"Semi: {tried:,} provati in {time.perf_counter()-t_seed:.2f}s "
                  f"({'1 processo' if pool is None else f'{_POOL_N} processi'})")

        # --- fusione delle regioni confinanti sulla STESSA superficie ------------
        tol_len = max(50.0 * self.tol_fit, 1e-5 * self.diag)
        changed = True
        while changed and len(found) > 1:
            changed = False
            owner = {}
            for k, (_, rf) in enumerate(found):
                for i in rf:
                    owner[i] = k
            for k in range(len(found)):
                pa, ra = found[k]
                if pa is None:
                    continue
                nbrs = {owner[j] for i in ra for j in topo.adj[i] if j in owner and owner[j] != k}
                for q in sorted(nbrs):
                    pb, rb = found[q]
                    if pb is None or not prims_equal(pa, pb, tol_len):
                        continue
                    union = sorted(set(ra) | set(rb))
                    p = refit_exact(union, topo.verts, topo.norms, topo.areas, pa.kind)
                    if p is None:
                        continue
                    if abs(p.slope) < 1e-9:
                        p.slope = 0.0
                    P = np.vstack([topo.verts[i] for i in union])
                    if float(np.abs(p.dist(P)).max()) > self.tol_fit:
                        continue
                    found[k] = (p, union)
                    found[q] = (None, [])
                    changed = True
                    break
                if changed:
                    break
            found = [(p, rf) for p, rf in found if p is not None]

        found = self.relabel(found)
        regions = [describe_region(p, topo, rf) for p, rf in found]
        # ⚠️ faccette che coprono piu' di 30 gradi ciascuna non sono una
        # tassellatura di quella superficie: e' un fit casuale su 4 facce
        regions = [R for R in regions
                   if not (R.prim.kind in (AXIAL, TORUS) and R.coverage > 0
                           and R.coverage * 360.0 / max(len(R.faces), 1) > 30.0)]
        # ⚠️ frammenti: un "cilindro" di 5 faccette che copre 7 gradi, o un
        # toro con raggio maggiore di 0.3 mm, non e' una lavorazione: e' un fit
        # casuale su rumore, e convertito produce una scaglia curva fra facce
        # lisce (gli artefatti a scalino). Restano tassellati.
        def _fragment(R):
            # ⚠️ una fascia stretta ma LUNGA (60 faccette su 10 gradi) e' un
            # raccordo vero: il numero di faccette conta quanto la copertura
            if R.prim.kind in (AXIAL, TORUS) and not R.closed_u and R.coverage * 360.0 < 15.0                     and len(R.faces) < 12:
                return True
            if R.prim.kind == TORUS and (R.prim.r0 < 0.3 * R.prim.r1
                                         or (len(R.faces) < 6 and R.coverage * 360.0 < 30.0)):
                return True
            if R.prim.kind == SPHERE and R.coverage < 0.005 and len(R.faces) < 10:
                return True
            return False
        regions = [R for R in regions if not _fragment(R)]
        regions.sort(key=lambda R: -sum(topo.areas[i] for i in R.faces))
        Log.info(f"Semi provati {tried:,} · regioni curve trovate {len(regions)} · "
                 f"faccette coinvolte {sum(len(R.faces) for R in regions):,} / {nF:,}")
        return regions


    def relabel(self, found):
        """
        Riassegnazione competitiva delle faccette di confine.

        ⚠️ Due superfici TANGENTI (sfera d'angolo e raccordo) sono
        indistinguibili vicino alla giunzione: la fila di triangolini sottili
        della sfera sta entro tolleranza anche dal cilindro, e la prima
        regione che cresce se la prende. Il confine fra le due regioni si
        sposta di una fila e la curva di giunzione non e' piu' il cerchio
        esatto. Qui ogni faccetta di confine va alla regione la cui
        primitiva la spiega MEGLIO (residuo piu' basso), poi si rifitta.
        """
        topo = self.topo
        if len(found) < 2:
            return found
        prims = [p for p, _ in found]
        sets = [set(rf) for _, rf in found]
        owner = {}
        for k, s in enumerate(sets):
            for i in s:
                owner[i] = k
        moved_total = 0
        for _ in range(4):
            moved = 0
            for i in list(owner):
                a = owner[i]
                nb_regions = {owner[j] for j in topo.adj[i] if j in owner and owner[j] != a}
                if not nb_regions:
                    continue
                V = topo.verts[i]
                ra = float(np.abs(prims[a].dist(V)).max())
                best, rb = a, ra
                for b in nb_regions:
                    r_ = float(np.abs(prims[b].dist(V)).max())
                    if r_ <= self.tol_fit and r_ < 0.5 * rb:
                        best, rb = b, r_
                if best != a:
                    sets[a].discard(i)
                    sets[best].add(i)
                    owner[i] = best
                    moved += 1
            if not moved:
                break
            moved_total += moved
            for k in range(len(found)):
                if len(sets[k]) >= 3:
                    p2 = refit_exact(sorted(sets[k]), topo.verts, topo.norms, topo.areas, prims[k].kind)
                    if p2 is not None:
                        if abs(p2.slope) < 1e-9:
                            p2.slope = 0.0
                        prims[k] = p2
        if moved_total:
            Log.debug(f"Riassegnate {moved_total} faccette di confine fra regioni tangenti")
        out = []
        for k in range(len(found)):
            rf = largest_component(sorted(sets[k]), topo.adj)
            if len(rf) >= self.min_faces:
                out.append((prims[k], rf))
        return out


# =============================================================================
# 8b-bis. SEMI IN PARALLELO
#
# ⚠️ PERCHE' A PROCESSI E NON A THREAD. Le chiamate a OpenCascade e il codice
# numpy di questo script tengono il GIL (misurato: otto calcoli di volume su
# quattro thread costano quanto in sequenza), quindi i thread non fanno
# guadagnare niente. Gli unici pezzi davvero parallelizzabili sono quelli che
# lavorano solo su array numpy: la ricerca delle primitive dai semi, che e' la
# fetta piu' grossa delle Fasi B e C. Le sostituzioni nel solido restano in
# sequenza: modificano una struttura OCC condivisa e vanno verificate una alla
# volta.
# =============================================================================


class TopoLite:
    """I soli campi della topologia che servono alla segmentazione: numpy e
    liste, quindi trasferibili a un altro processo."""

    __slots__ = ("nF", "verts", "norms", "areas", "nverts", "planar", "adj")

    def __init__(self, topo):
        n = topo.nF
        self.nF = n
        self.verts = [topo.verts[i] for i in range(n)]
        self.norms = [topo.norms[i] for i in range(n)]
        self.areas = [topo.areas[i] for i in range(n)]
        self.nverts = [topo.nverts[i] for i in range(n)]
        self.planar = [topo.planar[i] for i in range(n)]
        self.adj = [sorted(topo.adj[i]) for i in range(n)]

    def __getstate__(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __setstate__(self, st):
        for k, v in st.items():
            setattr(self, k, v)


_POOL = None
_POOL_N = 0
_W_KEY = None
_W_SEG = None


def _seed_pool(threads: int, want: Optional[int] = None):
    """
    Pool di processi, creato una volta sola e riusato da tutte le fasi.

    ⚠️ QUANTI PROCESSI. Non "tutti quelli che ha la macchina": accendere un
    interprete che importa OpenCascade costa quasi un secondo, e su un pezzo
    da mezzo secondo di semi ventidue processi ci mettono piu' tempo a
    partire di quanto ne facciano risparmiare (misurato: test.stl 4.2 s con
    quattro processi, 6.1 s con ventidue). Quindi il numero si proporziona al
    lavoro da fare, con -j come tetto.
    """
    global _POOL, _POOL_N
    if threads is None or threads < 2:
        return None
    if _POOL is not None:
        return _POOL
    k = int(threads) if want is None else int(max(2, min(int(threads), int(want))))
    try:
        from concurrent.futures import ProcessPoolExecutor
        _POOL = ProcessPoolExecutor(max_workers=k)
        _POOL_N = k
    except Exception as ex:
        Log.warn(f"niente multiprocessing ({type(ex).__name__}: {ex}): si usa un solo core")
        _POOL, _POOL_N = None, 0
    return _POOL


def _worker_boot():
    return os.getpid()


def warm_pool(threads: int, n_faces: int = 0) -> None:
    """
    Accende i processi PRIMA che servano.

    ⚠️ Ogni processo di lavoro e' un interprete nuovo che deve importare
    OpenCascade: quasi un secondo. Se li si accende quando servono, quel
    secondo si paga tutto insieme e su un pezzo piccolo mangia il guadagno.
    Accesi qui, bootano mentre la Fase A lavora su un core solo.
    """
    pool = _seed_pool(threads, want=n_faces // 5000)
    if pool is None:
        return
    try:
        for _ in range(_POOL_N):
            pool.submit(_worker_boot)
    except Exception:
        pass
    Log.debug(f"Processi di lavoro accesi: {_POOL_N}")


def close_pool() -> None:
    global _POOL, _POOL_N
    if _POOL is not None:
        try:
            _POOL.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    _POOL, _POOL_N = None, 0


def _worker_seeds(task):
    """Prova una lista di semi su una fotografia delle faccette gia' prese."""
    import pickle
    global _W_KEY, _W_SEG
    key, path, taken_b, seeds = task
    if _W_KEY != key or _W_SEG is None:
        with open(path, "rb") as fh:
            lite, kw = pickle.load(fh)
        _W_SEG = Segmenter(lite, **kw)
        _W_KEY = key
    seg = _W_SEG
    seg.taken = [b != 0 for b in taken_b]
    out = []
    for s in seeds:
        if seg.taken[s]:
            continue
        try:
            res, why = seg.try_seed(s)
        except Exception as ex:
            out.append((s, None, [f"{type(ex).__name__}: {ex}"]))
            continue
        if res is None:
            out.append((s, None, []))
            continue
        prim, keep = res
        # ⚠️ il processo si segna quello che ha preso: senza questo rifarebbe
        # la stessa regione partendo da ogni faccetta che ne fa parte
        for i in keep:
            seg.taken[i] = True
        out.append((s, prim, keep))
    return out


def segment_curved(topo: Topo, tol_fit: float, tol_grow: float, diag: float,
                   min_faces: int = 4, grow_angle: float = 35.0,
                   seed_smooth: float = 20.0, allow_sphere: bool = True,
                   allow_cone: bool = True, allow_torus: bool = True,
                   only_cyl: bool = False, threads: int = 1) -> List[Region]:
    return Segmenter(topo, tol_fit, tol_grow, diag, min_faces, grow_angle, seed_smooth,
                     allow_sphere, allow_cone, allow_torus, only_cyl, threads).run()



# =============================================================================
# 9. SUPERFICI ANALITICHE: parametrizzazione, pcurve, spigoli
# =============================================================================
#
# ⚠️ Le pcurve (curve nello spazio (u,v) della superficie) le calcoliamo NOI,
# non il proiettore di OCC. Il proiettore riporta u in [0, 2pi): un bordo che
# attraversa il seam (u = 0) viene spezzato, il wire non chiude nello spazio
# dei parametri e la faccia esce non valida o, peggio, come COMPLEMENTO della
# regione (una calotta di 18 mm2 che diventa una sfera intera). Campionando
# la curva 3D, proiettando con la nostra parametrizzazione e SVOLGENDO u e v
# lungo il wire, il seam non esiste piu': ogni wire e' continuo per costruzione.


class SurfParam:
    """Parametrizzazione esplicita (identica a quella di Geom_*Surface)."""

    def __init__(self, prim: "Prim", ref: Optional[np.ndarray] = None,
                 pole_axis: Optional[np.ndarray] = None):
        # ⚠️ cono con semiangolo negativo: lo STEP lo scrive come
        # SURFACE_OF_REVOLUTION generica. Si gira l'asse (e il segno della
        # pendenza) su una COPIA: stessa geometria, semiangolo positivo.
        if prim.kind == AXIAL and prim.slope < 0:
            prim = Prim(AXIAL, prim.center.copy(), -prim.axis, prim.r0, -prim.slope)
        self.prim = prim
        k = prim.kind
        C = np.asarray(prim.center, float)
        if k == PLANE:
            Z = prim.axis / np.linalg.norm(prim.axis)
            X, Y = ortho_frame(Z)
        elif k == SPHERE:
            if pole_axis is None:
                pole_axis = np.array([0.0, 0.0, 1.0])
            Z = np.asarray(pole_axis, float)
            Z = Z / np.linalg.norm(Z)
            if ref is not None:
                X = np.asarray(ref, float) - float(np.asarray(ref, float) @ Z) * Z
            else:
                X = ortho_frame(Z)[0]
            if np.linalg.norm(X) < 1e-9:
                X = ortho_frame(Z)[0]
            X = X / np.linalg.norm(X)
            Y = np.cross(Z, X)
        else:
            Z = prim.axis / np.linalg.norm(prim.axis)
            if ref is not None:
                X = np.asarray(ref, float) - float(np.asarray(ref, float) @ Z) * Z
            else:
                X = ortho_frame(Z)[0]
            if np.linalg.norm(X) < 1e-9:
                X = ortho_frame(Z)[0]
            X = X / np.linalg.norm(X)
            Y = np.cross(Z, X)
        self.C, self.X, self.Y, self.Z = C, X, Y, Z
        self.periodic_u = k != PLANE
        self.periodic_v = k == TORUS
        if k == AXIAL:
            self.alpha = math.atan(prim.slope)

    # --- 3D -> (u, v) --------------------------------------------------------
    def uv(self, P: np.ndarray):
        P = np.atleast_2d(P)
        d = P - self.C
        k = self.prim.kind
        if k == PLANE:
            return d @ self.X, d @ self.Y
        t = d @ self.Z
        x, y = d @ self.X, d @ self.Y
        if k == SPHERE:
            r = np.maximum(np.linalg.norm(d, axis=1), 1e-300)
            return np.arctan2(y, x), np.arcsin(np.clip(t / r, -1.0, 1.0))
        u = np.arctan2(y, x)
        if k == TORUS:
            rho = np.hypot(x, y)
            return u, np.arctan2(t, rho - self.prim.r0)
        if abs(self.prim.slope) < 1e-12:
            return u, t
        return u, t / math.cos(self.alpha)

    # --- (u, v) -> 3D --------------------------------------------------------
    def point(self, u, v):
        u = np.atleast_1d(np.asarray(u, float))
        v = np.atleast_1d(np.asarray(v, float))
        k = self.prim.kind
        cu, su = np.cos(u)[:, None], np.sin(u)[:, None]
        radial = cu * self.X + su * self.Y
        if k == PLANE:
            return self.C + u[:, None] * self.X + v[:, None] * self.Y
        if k == SPHERE:
            r = self.prim.r0
            return self.C + r * np.cos(v)[:, None] * radial + r * np.sin(v)[:, None] * self.Z
        if k == TORUS:
            R, r = self.prim.r0, self.prim.r1
            return (self.C + (R + r * np.cos(v))[:, None] * radial
                    + r * np.sin(v)[:, None] * self.Z)
        if abs(self.prim.slope) < 1e-12:
            return self.C + self.prim.r0 * radial + v[:, None] * self.Z
        sa, ca = math.sin(self.alpha), math.cos(self.alpha)
        return self.C + (self.prim.r0 + v * sa)[:, None] * radial + (v * ca)[:, None] * self.Z

    def geom(self):
        ax3 = gp_Ax3(_mk_pnt(self.C), _mk_dir(self.Z), _mk_dir(self.X))
        k = self.prim.kind
        if k == PLANE:
            return Geom_Plane(ax3)
        if k == SPHERE:
            return Geom_SphericalSurface(ax3, float(self.prim.r0))
        if k == TORUS:
            return Geom_ToroidalSurface(ax3, float(self.prim.r0), float(self.prim.r1))
        if abs(self.prim.slope) < 1e-12:
            return Geom_CylindricalSurface(ax3, float(self.prim.r0))
        return Geom_ConicalSurface(ax3, float(self.alpha), float(self.prim.r0))

    def iso_u_curve(self, u: float):
        """Curva 3D a u costante (generatrice o meridiano): (Geom_Curve, param==v)."""
        k = self.prim.kind
        radial = math.cos(u) * self.X + math.sin(u) * self.Y
        if k == AXIAL:
            if abs(self.prim.slope) < 1e-12:
                return Geom_Line(_mk_pnt(self.C + self.prim.r0 * radial), _mk_dir(self.Z))
            sa, ca = math.sin(self.alpha), math.cos(self.alpha)
            return Geom_Line(_mk_pnt(self.C + self.prim.r0 * radial),
                             _mk_dir(sa * radial + ca * self.Z))
        if k == TORUS:
            c = self.C + self.prim.r0 * radial
            ax2 = gp_Ax2(_mk_pnt(c), _mk_dir(np.cross(radial, self.Z)), _mk_dir(radial))
            return Geom_Circle(ax2, float(self.prim.r1))
        if k == SPHERE:
            ax2 = gp_Ax2(_mk_pnt(self.C), _mk_dir(np.cross(radial, self.Z)), _mk_dir(radial))
            return Geom_Circle(ax2, float(self.prim.r0))
        return None


class CGeom(_Curve):
    """
    Curva di intersezione GENERICA (B-spline di GeomAPI_IntSS) avvolta con
    la stessa interfaccia delle curve analitiche: serve per il confine fra
    due raccordi con angoli diversi o fra un raccordo e uno smusso tondo,
    dove l'intersezione non e' ne' retta ne' cerchio. Senza questa il confine
    restava la scaletta della mesh fra due facce lisce.
    """
    period = None

    def __init__(self, curve):
        self.c = curve
        self._proj = _m("GeomAPI").GeomAPI_ProjectPointOnCurve

    def param(self, P):
        out = []
        for p in np.atleast_2d(P):
            pr = self._proj(_mk_pnt(p), self.c)
            out.append(float(pr.LowerDistanceParameter()) if pr.NbPoints() > 0 else float("nan"))
        return np.array(out)

    def point(self, t):
        t = np.atleast_1d(t)
        return np.array([[self.c.Value(float(x)).X(), self.c.Value(float(x)).Y(),
                          self.c.Value(float(x)).Z()] for x in t])

    def dist(self, P):
        out = []
        for p in np.atleast_2d(P):
            pr = self._proj(_mk_pnt(p), self.c)
            out.append(float(pr.LowerDistance()) if pr.NbPoints() > 0 else float("inf"))
        return np.array(out)

    def to_geom(self):
        return self.c

    def label(self):
        return "intersezione"


def intersection_curves(surf_a, surf_b, tol: float) -> List[_Curve]:
    """Curve di intersezione fra due Geom_Surface (GeomAPI_IntSS)."""
    out: List[_Curve] = []
    try:
        ss = _m("GeomAPI").GeomAPI_IntSS(surf_a, surf_b, float(tol))
        if ss.IsDone():
            for i in range(1, ss.NbLines() + 1):
                c = ss.Line(i)
                if c is not None and not c.IsNull() if hasattr(c, "IsNull") else c is not None:
                    out.append(CGeom(_keep(c)))
    except Exception as e:
        Log.debug(f"IntSS: {e}")
    return out


def _unwrap_to(vals: np.ndarray, start: float, period: float) -> np.ndarray:
    """Svolge una sequenza periodica in modo che il primo valore sia vicino a start."""
    out = np.unwrap(vals, period=period)
    k = round((start - out[0]) / period)
    return out + k * period


def curve_points(curve, t0: float, t1: float, n: int) -> np.ndarray:
    ts = np.linspace(t0, t1, n)
    return np.array([[curve.Value(t).X(), curve.Value(t).Y(), curve.Value(t).Z()]
                     for t in ts]), ts


def make_pcurve(sp: SurfParam, curve, t0: float, t1: float, traverse_fwd: bool,
                u_prev: Optional[float], v_prev: Optional[float], n: int = 25):
    """
    Pcurve dello spigolo (curva 3D `curve` fra t0 e t1) sulla superficie sp.
    Ritorna (Geom2d_Curve, (u_end, v_end) nel verso di percorrenza, deviazione).
    u_prev/v_prev = fine dello spigolo precedente nel wire: la pcurve viene
    svolta per partire di li'.
    """
    P, ts = curve_points(curve, t0, t1, n)
    u, v = sp.uv(P)
    if not traverse_fwd:
        u, v = u[::-1], v[::-1]
    if sp.periodic_u:
        u = _unwrap_to(u, u_prev if u_prev is not None else float(u[0]), 2 * math.pi)
    if sp.periodic_v:
        v = _unwrap_to(v, v_prev if v_prev is not None else float(v[0]), 2 * math.pi)
    u_end, v_end = float(u[-1]), float(v[-1])
    if not traverse_fwd:
        u, v = u[::-1], v[::-1]
    # retta in (u,v) e lineare in t?  -> B-spline di grado 1 (esatta)
    lin_u = u[0] + (u[-1] - u[0]) * (ts - t0) / max(t1 - t0, 1e-300)
    lin_v = v[0] + (v[-1] - v[0]) * (ts - t0) / max(t1 - t0, 1e-300)
    if float(np.abs(lin_u - u).max()) < 1e-9 and float(np.abs(lin_v - v).max()) < 1e-9:
        poles = _TColgp.TColgp_Array1OfPnt2d(1, 2)
        poles.SetValue(1, gp_Pnt2d(float(u[0]), float(v[0])))
        poles.SetValue(2, gp_Pnt2d(float(u[-1]), float(v[-1])))
        knots = _TColStd.TColStd_Array1OfReal(1, 2)
        knots.SetValue(1, float(t0)); knots.SetValue(2, float(t1))
        mults = _TColStd.TColStd_Array1OfInteger(1, 2)
        mults.SetValue(1, 2); mults.SetValue(2, 2)
        c2d = Geom2d_BSplineCurve(poles, knots, mults, 1)
    else:
        pts = _TColgp.TColgp_HArray1OfPnt2d(1, n)
        prm = _TColStd.TColStd_HArray1OfReal(1, n)
        for i in range(n):
            pts.SetValue(i + 1, gp_Pnt2d(float(u[i]), float(v[i])))
            prm.SetValue(i + 1, float(ts[i]))
        it = _Geom2dAPI.Geom2dAPI_Interpolate(pts, prm, False, 1e-12)
        it.Perform()
        if not it.IsDone():
            return None, (u_end, v_end), math.inf
        c2d = it.Curve()
    # deviazione "same parameter": |C3d(t) - S(pcurve(t))| su un campione fitto
    tt = np.linspace(t0, t1, 2 * n + 1)
    Q = np.array([[curve.Value(t).X(), curve.Value(t).Y(), curve.Value(t).Z()] for t in tt])
    uu = np.array([c2d.Value(t).X() for t in tt])
    vv = np.array([c2d.Value(t).Y() for t in tt])
    dev = float(np.linalg.norm(Q - sp.point(uu, vv), axis=1).max())
    return c2d, (u_end, v_end), dev


def edge_curve(edge):
    """(Geom_Curve, t0, t1) dello spigolo, con la location applicata."""
    e = td_Edge(edge)
    loc = TopLoc_Location()
    c = bt_Curve(e, loc, 0.0, 0.0)
    t0, t1 = bt_Range(e)
    if c is None:
        return None, t0, t1
    if not loc.IsIdentity():
        c = c.Transformed(loc.Transformation())
    return c, float(t0), float(t1)


def edge_endpoints(edge) -> Tuple[np.ndarray, np.ndarray]:
    e = td_Edge(edge)
    return vpos(te_FirstVertex(e)), vpos(te_LastVertex(e))


def edge_is_analytic(edge) -> bool:
    try:
        return BRepAdaptor_Curve(td_Edge(edge)).GetType() in (GeomAbs_Line, GeomAbs_Circle,
                                                             GeomAbs_Ellipse)
    except Exception:
        return False


def make_edge_on_curve(cv: "_Curve", V1, V2, P1: np.ndarray, P2: np.ndarray,
                       Pmid: Optional[np.ndarray], closed: bool,
                       Pnext: Optional[np.ndarray] = None):
    """
    Spigolo analitico fra due vertici ESISTENTI (condivisi coi vicini).
    Per un cerchio/ellisse l'arco giusto e' quello che passa dal vertice
    intermedio della catena (Pmid). Ritorna (spigolo, fwd): fwd = True se lo
    spigolo FORWARD va nel verso di percorrenza della catena (da V1).
    Per una catena CHIUSA il verso si legge dal secondo vertice (Pnext).
    """
    g = _keep(cv.to_geom())
    if closed:
        t0 = float(cv.param(P1[None, :])[0])
        me = _keep(BRepBuilderAPI_MakeEdge(g, V1, V1, t0, t0 + cv.period))
        fwd = True
        if Pnext is not None:
            dt = (float(cv.param(Pnext[None, :])[0]) - t0) % cv.period
            fwd = dt < 0.5 * cv.period
        return (me.Edge() if me.IsDone() else None), fwd
    t1 = float(cv.param(P1[None, :])[0])
    t2 = float(cv.param(P2[None, :])[0])
    if cv.period:
        while t2 <= t1:
            t2 += cv.period
        if Pmid is not None:
            tm = float(cv.param(Pmid[None, :])[0])
            while tm < t1:
                tm += cv.period
            if tm > t2:                       # l'arco corto non passa da Pmid
                t1, t2 = t2 - cv.period, t1
                V1, V2 = V2, V1
                fwd_is_v1 = False
            else:
                fwd_is_v1 = True
        else:
            if t2 - t1 > math.pi:             # preferisci l'arco corto
                t1, t2 = t2 - cv.period, t1
                V1, V2 = V2, V1
                fwd_is_v1 = False
            else:
                fwd_is_v1 = True
    else:
        if t2 < t1:
            t1, t2 = t2, t1
            V1, V2 = V2, V1
            fwd_is_v1 = False
        else:
            fwd_is_v1 = True
    if t2 - t1 < 1e-12:
        return None, fwd_is_v1
    me = _keep(BRepBuilderAPI_MakeEdge(g, V1, V2, t1, t2))
    if not me.IsDone():
        return None, fwd_is_v1
    return me.Edge(), fwd_is_v1


def composed_edge_orientations(face, edge) -> List:
    """
    Orientamenti dello spigolo nella faccia COMPOSTI con quelli di wire e
    faccia (TopExp_Explorer restituisce solo quello memorizzato nel wire).
    In un guscio coerente ogni spigolo e' FORWARD in una faccia e REVERSED
    nell'altra, in senso composto.
    """
    out = []
    f_rev = face.Orientation() == TopAbs_REVERSED
    it_w = _TopoDS.TopoDS_Iterator(face, False, True)
    while it_w.More():
        w = it_w.Value()
        w_rev = w.Orientation() == TopAbs_REVERSED
        it_e = _TopoDS.TopoDS_Iterator(w, False, True)
        while it_e.More():
            ed = it_e.Value()
            if ed.IsSame(edge):
                e_rev = ed.Orientation() == TopAbs_REVERSED
                out.append(TopAbs_REVERSED if (f_rev ^ w_rev ^ e_rev) else TopAbs_FORWARD)
            it_e.Next()
        it_w.Next()
    return out


def grow_vertex_tolerance(V, dist: float) -> None:
    cur = float(bt_Tolerance(td_Vertex(V)))
    need = 1.2 * dist + 1e-7
    if need > cur:
        BRep_Builder().UpdateVertex(td_Vertex(V), need)


# =============================================================================
# 10. MOTORE DI SOSTITUZIONE LOCALE (una regione alla volta, con rollback)
# =============================================================================
#
# Per ogni regione:
#   1. bordo -> catene (spigoli consecutivi con lo stesso vicino);
#   2. ogni catena: se il vicino e' analitico e i vertici stanno sulla curva di
#      intersezione esatta -> UNO spigolo analitico nuovo fra i vertici
#      ESISTENTI; altrimenti si tengono gli spigoli poligonali cosi' come sono
#      (condivisi col vicino tassellato, pcurve aggiunta sul posto);
#   3. faccia nuova = superficie + wire (+ seam se chiusa a 360 gradi);
#   4. controlli sulla faccia (validita', area);
#   5. BRepTools_ReShape: facce della regione -> faccia nuova, catene -> spigoli
#      nuovi anche nei vicini;
#   6. controlli sul solido (spigoli liberi invariati, facce vicine valide,
#      orientamento coerente, volume entro lo sfrido delle corde);
#   7. se un controllo fallisce: la shape resta quella di prima.


@dataclass
class Chain:
    edges: List[int]              # indici spigolo (Topo), in ordine di percorrenza
    fwd: List[bool]               # True = percorso da FirstVertex a LastVertex
    verts: List[int]              # vertici in ordine (len = edges+1; chiusa: primo==ultimo)
    nb: int                       # faccia vicina (indice Topo), -1 = bordo libero
    closed: bool = False


@dataclass
class Loop:
    chains: List[Chain]


class Engine:
    def __init__(self, shape, tol_fit: float, diag: float, max_edge_tol: float,
                 allow_polyline: bool = True, verbose: bool = False):
        self.shape = shape
        self.tol_fit = tol_fit
        self.tol_curve = 4.0 * tol_fit
        self.diag = diag
        self.max_edge_tol = max_edge_tol
        self.allow_polyline = allow_polyline
        self.verbose = verbose
        self.registry = TopTools_IndexedMapOfShape()     # facce analitiche curve
        self.analytic: Dict[int, "Prim"] = {}            # id registro -> Prim
        self.topo = Topo(shape)
        self.free0 = count_free_edges(shape)
        self.vol0 = shape_volume(shape)
        self.n_ok = 0
        self.n_fail = 0
        self.log: List[str] = []

    # --- primitive analitiche dei vicini --------------------------------------
    def prim_of_face(self, i: int) -> Optional["Prim"]:
        f = self.topo.faces[i]
        k = self.registry.FindIndex(f)
        if k > 0 and k in self.analytic:
            return self.analytic[k]
        return plane_prim_of_face(self.topo, i)

    def _register(self, face, prim):
        k = self.registry.Add(face)
        self.analytic[k] = prim

    # --- catene di bordo -------------------------------------------------------
    def region_loops(self, rf: List[int]) -> Tuple[Optional[List[Loop]], str]:
        topo = self.topo
        rset = set(rf)
        bnd: Dict[int, int] = {}                 # spigolo -> faccia di regione
        for i in rf:
            for k in topo.f_edges[i]:
                fs = topo.e_faces[k]
                inside = [f for f in fs if f in rset]
                if len(fs) < 2:
                    return None, "bordo libero della mesh"
                if len(inside) == 1:
                    bnd[k] = i
        if not bnd:
            return None, "nessun bordo"
        # verso di percorrenza: interno della faccia a sinistra rispetto alla
        # normale uscente (indipendente dalle convenzioni di orientamento OCC)
        dir_edge: Dict[int, Tuple[int, int]] = {}
        out_v: Dict[int, List[int]] = defaultdict(list)
        for k, i in bnd.items():
            a, b = topo.e_verts[k]
            A, B = topo.vpos[a], topo.vpos[b]
            n = topo.norms[i]
            inward = np.cross(n, B - A)
            if float((topo.cents[i] - 0.5 * (A + B)) @ inward) < 0:
                a, b = b, a
            dir_edge[k] = (a, b)
            out_v[a].append(k)
        # percorrenza con rotazione attorno al vertice (gestisce i nodi a 4)
        def next_edge(k_in: int) -> Optional[int]:
            a, b = dir_edge[k_in]
            cands = out_v.get(b, [])
            if len(cands) == 1:
                return cands[0]
            if not cands:
                return None
            f = bnd[k_in]
            seen = set()
            for _ in range(64):
                es_at_b = [e for e in topo.f_edges[f] if b in topo.e_verts[e] and e != k_in]
                es_at_b = [e for e in es_at_b if e not in seen]
                if not es_at_b:
                    return None
                e = es_at_b[0]
                seen.add(e)
                if e in bnd:
                    return e if bnd[e] == f and dir_edge[e][0] == b else None
                nxt = [g for g in topo.e_faces[e] if g in rset and g != f]
                if not nxt:
                    return None
                f, k_in = nxt[0], e
            return None

        used = set()
        loops: List[List[int]] = []
        for k0 in sorted(bnd):
            if k0 in used:
                continue
            seq, k = [], k0
            while k is not None and k not in used:
                used.add(k)
                seq.append(k)
                k = next_edge(k)
            if k != k0 and (not seq or dir_edge[seq[-1]][1] != dir_edge[seq[0]][0]):
                return None, "bordo non richiudibile"
            loops.append(seq)
        # catene: si spezza dove cambia il vicino
        out: List[Loop] = []
        for seq in loops:
            nbs = []
            for k in seq:
                fs = [f for f in topo.e_faces[k] if f not in rset]
                nbs.append(fs[0] if fs else -1)
            n = len(seq)
            starts = [j for j in range(n) if nbs[j] != nbs[j - 1]]
            chains: List[Chain] = []
            if not starts:
                ch = Chain([], [], [dir_edge[seq[0]][0]], nbs[0], closed=True)
                for k in seq:
                    ch.edges.append(k)
                    ch.fwd.append(dir_edge[k][0] == topo.e_verts[k][0])
                    ch.verts.append(dir_edge[k][1])
                chains.append(ch)
            else:
                for si, s in enumerate(starts):
                    e_ = starts[(si + 1) % len(starts)]
                    idxs = list(range(s, e_)) if e_ > s else list(range(s, n)) + list(range(0, e_))
                    ch = Chain([], [], [dir_edge[seq[idxs[0]]][0]], nbs[s])
                    for j in idxs:
                        k = seq[j]
                        ch.edges.append(k)
                        ch.fwd.append(dir_edge[k][0] == topo.e_verts[k][0])
                        ch.verts.append(dir_edge[k][1])
                    chains.append(ch)
            out.append(Loop(chains))
        return out, ""

    # --- conversione di una regione -------------------------------------------
    def convert(self, R: Region, strict_hole: bool = False) -> Tuple[bool, str]:
        mt0 = max_tolerance(self.shape) if self.verbose else 0.0
        ok, why = self._convert(R, strict_hole)
        # ⚠️ SECONDA STRATEGIA: se il vicino piano ricostruito con l'arco esatto
        # esce non valido (o l'orientamento non torna, che e' lo stesso caso
        # visto dall'altro tentativo), si riprova lasciando POLIGONALI i bordi
        # coi piani: la faccia analitica si converte lo stesso, il bordo resta
        # quello della mesh.
        retry = ("vicina" in why or "orientamento" in why or "volume" in why)
        if not ok and not strict_hole and retry:
            ok2, why2 = self._convert(R, strict_hole, analytic_planes=False)
            if ok2:
                ok, why = ok2, why2
                R.note += " · bordi coi piani lasciati poligonali"
            elif "vicina" in why2 or "orientamento" in why2 or "volume" in why2:
                # ⚠️ TERZA STRATEGIA: nessuno spigolo nuovo, da nessuna parte.
                # Serve quando il vicino e' una faccia analitica CHIUSA (un foro
                # gia' convertito, col suo seam): rifarle il bordo la rompe
                # ("wire:NotConnected"). Cosi' la regione diventa comunque una
                # superficie esatta e il contorno resta IDENTICO alla mesh:
                # nessun vicino viene toccato, nessuna scheggia nuova.
                ok3, why3 = self._convert(R, strict_hole, analytic_planes=False,
                                          analytic_curved=False)
                if ok3:
                    ok, why = ok3, why3
                    R.note += " · contorno lasciato poligonale"
        if self.verbose:
            mt1 = max_tolerance(self.shape)
            if mt1 > max(mt0 * 1.5, self.max_edge_tol):
                Log.debug(f"    !! tolleranza massima salita da {mt0:.1e} a {mt1:.1e} "
                          f"({'accettata' if ok else 'scartata'})")
        if ok:
            self.n_ok += 1
            R.status = "OK"
        else:
            self.n_fail += 1
            R.status = "scartata: " + why
        if self.verbose or not ok:
            Log.debug(f"  {R.label()} -> {R.status}")
        return ok, why

    def _convert(self, R: Region, strict_hole: bool, analytic_planes: bool = True,
                 analytic_curved: bool = True) -> Tuple[bool, str]:
        topo = self.topo
        prim = R.prim
        rf = [topo.face_index(f) for f in R.fobjs]
        if any(i < 0 for i in rf):
            return False, "facce della regione non piu' presenti"
        rset = set(rf)
        self._rset = rset
        self._rf = rf
        loops, why = self.region_loops(rf)
        # ⚠️ BORDO NON RICHIUDIBILE: quasi sempre un vertice di strozzatura, dove
        # due tratti del bordo della stessa regione si toccano (faccette a
        # sliver). Si tolgono le faccette che passano da quei vertici, si tiene
        # la componente piu' grande e si riprova: il grosso della regione si
        # converte, gli sliver restano tassellati.
        for _ in range(3):
            if loops is not None:
                break
            pinch = self._pinch_vertices(rf)
            if not pinch:
                break
            keep_ = [i for i in rf if not (set(topo.e_verts[k][0] for k in topo.f_edges[i])
                                           | set(topo.e_verts[k][1] for k in topo.f_edges[i])) & pinch]
            keep_ = largest_component(keep_, topo.adj)
            if len(keep_) < max(4, len(rf) // 2):
                break
            rf = keep_
            rset = set(rf)
            self._rset = rset
            self._rf = rf
            R.faces = [i for i in rf]
            R.fobjs = [topo.faces[i] for i in rf]
            loops, why = self.region_loops(rf)
        if loops is None:
            return False, why
        if strict_hole:
            if not (prim.kind == AXIAL and abs(prim.slope) < 1e-12 and R.closed_u and R.concave):
                return False, "non e' un foro cilindrico passante/cieco"
            if len(loops) != 2 or any(len(L.chains) != 1 or not L.chains[0].closed for L in loops):
                desc = "; ".join(
                    f"anello {k}: {len(L.chains)} catene, vicini "
                    + ",".join(("piano" if (ch.nb >= 0 and topo.planar[ch.nb] and topo.nverts[ch.nb] > 4)
                                else f"faccetta{topo.nverts[ch.nb] if ch.nb >= 0 else ''}")
                               for ch in L.chains[:6])
                    for k, L in enumerate(loops))
                return False, f"bordo del foro non e' fatto di due anelli semplici ({desc})"
            for L in loops:
                nb = L.chains[0].nb
                if nb < 0 or not topo.planar[nb]:
                    return False, "il foro non termina su facce piane"
                if abs(abs(float(topo.norms[nb] @ prim.axis)) - 1.0) > 1e-4:
                    return False, "faccia di sbocco non ortogonale all'asse"
        if R.closed_u and len(loops) != 2:
            return False, f"regione chiusa a 360 gradi con {len(loops)} anelli (attesi 2)"
        if not R.closed_u and prim.kind in (AXIAL, TORUS) and R.coverage > 0.97:
            return False, "copertura angolare ambigua"

        # --- superficie: cornice scelta lontano dal seam ------------------------
        C = np.array([topo.cents[i] for i in rf])
        W = np.array([max(topo.areas[i], 1e-12) for i in rf])
        ref = None
        pole_axis = None
        if prim.kind == SPHERE:
            d = C - prim.center
            d = d / np.maximum(np.linalg.norm(d, axis=1), 1e-12)[:, None]
            m = (d * W[:, None]).sum(axis=0) / W.sum()
            if float(np.linalg.norm(m)) < 0.2:
                return False, "sfera: regione troppo estesa (piu' di un emisfero)"
            ref = m / np.linalg.norm(m)
            # asse polare: ortogonale alla direzione media, il piu' lontano
            # possibile dai punti della regione (poli fuori dalla faccia)
            u_, v_ = ortho_frame(ref)
            phi = np.linspace(0.0, math.pi, 91)
            A = np.cos(phi)[:, None] * u_ + np.sin(phi)[:, None] * v_
            worst = np.abs(A @ d.T).max(axis=1)
            kbest = int(np.argmin(worst))
            if worst[kbest] > math.cos(math.radians(20.0)):
                return False, "sfera: polo troppo vicino alla regione"
            pole_axis = A[kbest]
        elif prim.kind in (AXIAL, TORUS):
            d = C - prim.center
            d = d - np.outer(d @ prim.axis, prim.axis)
            nn = np.linalg.norm(d, axis=1)
            good = nn > 1e-9
            if good.any():
                m = ((d[good] / nn[good, None]) * W[good, None]).sum(axis=0) / W[good].sum()
                if float(np.linalg.norm(m)) > 1e-6:
                    ref = m / np.linalg.norm(m)
        sp = SurfParam(prim, ref, pole_axis)
        surf = _keep(sp.geom())
        if R.closed_u:
            why = self._align_closed_chains(loops, sp)
            if why:
                return False, why

        # --- catene -> spigoli ----------------------------------------------------
        new_edges: Dict[int, Tuple[object, bool]] = {}     # id(chain) -> (edge, fwd)
        replaced: List[Tuple[Chain, object, bool]] = []
        n_analytic = n_poly = n_reused = 0
        vtol_backup: Dict[int, float] = {}

        def vertex_obj(j):
            return td_Vertex(topo.vmap.FindKey(j + 1))

        def bump_vertex(j, dist):
            V = vertex_obj(j)
            if j not in vtol_backup:
                vtol_backup[j] = float(bt_Tolerance(V))
            grow_vertex_tolerance(V, dist)

        med_area = float(np.median([topo.areas[i] for i in rf]))
        len_tot = len_debris = 0.0
        for L in loops:
            for ch in L.chains:
                Lc = float(sum(edge_length(topo.edges[k]) for k in ch.edges))
                len_tot += Lc
                if ch.nb >= 0 and topo.nverts[ch.nb] <= 4 and topo.areas[ch.nb] < 3.0 * med_area:
                    len_debris += Lc
        # ⚠️ REGIONE-FRAMMENTO IN MEZZO AL TASSELLATO. Una primitiva fittata su
        # quattro o cinque faccette, col bordo quasi tutto appoggiato ad altre
        # faccette sciolte, non e' una lavorazione del pezzo: e' il rumore
        # della zona di raccordo. Convertirla produce proprio le "tante facce
        # con forme irregolari" (raggi a caso, ø1.4589, ø2.0204...) che
        # sporcano il modello. Meglio lasciare li' la mesh: si rifinisce a mano.
        if (not strict_hole and len(rf) < 12
                and len_debris > 0.6 * max(len_tot, 1e-9)):
            return False, (f"regione troppo piccola in mezzo a faccette tassellate "
                           f"({len(rf)} facce, {100.0*len_debris/max(len_tot,1e-9):.0f}% "
                           f"del bordo): lasciata la mesh")
        for L in loops:
            for ch in L.chains:
                if ch.nb < 0:
                    return False, "bordo libero"
                if len(ch.edges) == 1 and not ch.closed:
                    # ⚠️ SMUSSO ACCANTO A RACCORDO. Uno spigolo solo di solito e'
                    # gia' buono e si riusa com'e'. Ma quando il vicino e' una
                    # fascia larga tassellata con un'unica corda (la fine di uno
                    # smusso, la quad alta 10 mm di un raccordo verticale), quella
                    # corda TAGLIA la superficie nuova: mezzo centesimo di
                    # millimetro sotto la sfera. La faccia allora viene scartata
                    # per tolleranza, e al suo posto resta la tassellatura. Qui si
                    # guarda quanto lo spigolo si stacca davvero dalla superficie:
                    # se e' piu' della tolleranza di fit, si prosegue e si prova a
                    # rifarlo con la curva esatta (sfera x piano = cerchio), che
                    # sta sia sulla faccia nuova sia sul piano del vicino.
                    _dv = self._edge_dev(ch.edges[0], prim)
                    if self.verbose:
                        Log.debug(f"    spigolo singolo L={edge_length(topo.edges[ch.edges[0]]):.4f} "
                                  f"scarto dalla superficie {_dv:.2e} (tol_fit {self.tol_fit:.2e}) "
                                  f"vicino f{ch.nb} area {topo.areas[ch.nb]:.3f}")
                    if _dv <= self.tol_fit:
                        n_reused += 1
                        continue
                if len(ch.edges) == 1 and ch.closed and edge_is_analytic(topo.edges[ch.edges[0]]):
                    n_reused += 1
                    continue
                nbp = self.prim_of_face(ch.nb)
                if not analytic_curved and nbp is not None and nbp.kind != PLANE:
                    nbp = None
                # ⚠️ un vicino piano grande quanto una faccetta non e' un piano
                # del pezzo: e' un pezzetto di superficie curva ridotta a piani
                # dalla Fase A. Sostituirgli il bordo con un arco esatto lo fa
                # auto-intersecare: si tiene la polilinea.
                if nbp is not None and nbp.kind == PLANE and                         (not analytic_planes or topo.areas[ch.nb] < 20.0 * med_area):
                    nbp = None
                cv = None
                P = topo.vpos[ch.verts]
                if nbp is not None:
                    cands = surf_surf_curves(prim, nbp, self.tol_fit)
                    cv = choose_curve(cands, P, self.tol_curve, scale=self.diag,
                                      arc_tol=max(self.tol_curve, 4.0 * R.sag))
                    # ⚠️ SUPERFICI TANGENTI (raccordo-piano, sfera-raccordo): la
                    # curva di intersezione esatta e' mal condizionata, un
                    # vertice a 1e-6 da entrambe le superfici puo' stare a
                    # 3e-3 dalla curva. Allora si prende la curva che passa
                    # per i vertici della mesh: e' coerente con l'ingresso, e
                    # la tolleranza dello spigolo ne misura lo scarto vero.
                    if cv is None and len(ch.verts) >= 4:
                        Pq = P[:-1] if ch.closed else P
                        cv = fit_curve(Pq, self.tol_curve, arc_tol=max(self.tol_curve, 4.0 * R.sag))
                    # ⚠️ vicino CURVO gia' convertito e nessuna curva analitica:
                    # intersezione generica delle due superfici (B-spline)
                    if cv is None and nbp.kind != PLANE and not ch.closed and len(ch.verts) >= 3:
                        # ⚠️ il confine della mesh fra due regioni e' una SCALETTA di
                        # faccette: i vertici interni stanno fino a una faccetta
                        # dalla curva vera. Contano solo gli ESTREMI (che restano
                        # come vertici): i vertici interni spariscono con la curva.
                        try:
                            sn = bt_Surface(topo.faces[ch.nb], TopLoc_Location())
                            gen = intersection_curves(surf, sn, self.tol_fit)
                            seg_len = float(np.median(np.linalg.norm(np.diff(P, axis=0), axis=1)))
                            loose = max(self.tol_curve, 2.0 * seg_len, 4.0 * R.sag)
                            cv = choose_curve(gen, P, loose, arc_tol=loose)
                            if self.verbose:
                                Log.debug(f"    IntSS: {len(gen)} curve, scarti "
                                          f"{[f'{float(np.abs(g.dist(P)).max()):.1e}/{arc_deviation(g, P):.1e}' for g in gen]} "
                                          f"tol {loose:.1e} -> {None if cv is None else 'ok'}")
                            if cv is not None:
                                d_ends = float(np.abs(cv.dist(P[[0, -1]])).max())
                                if d_ends > self.max_edge_tol:
                                    cv = None
                        except Exception as ex:
                            Log.debug(f"intersezione generica: {ex}")
                if self.verbose:
                    _c = surf_surf_curves(prim, nbp, self.tol_fit) if nbp is not None else []
                    _d = [f"{c.label()}:{float(np.abs(c.dist(P)).max()):.1e}/{arc_deviation(c, P):.1e}" for c in _c]
                    _f = fit_curve(P[:-1] if ch.closed else P, self.tol_curve, arc_tol=max(self.tol_curve, 4.0 * R.sag))
                    Log.debug(f"    catena {len(ch.edges)} spigoli, vicino {'-' if nbp is None else nbp.label()}"
                              f"{'' if nbp is None or nbp.kind != PLANE else f' area {topo.areas[ch.nb]:.2f}'}: "
                              f"candidate {_d} fit {None if _f is None else _f.label()} "
                              f"-> {None if cv is None else cv.label()}  (tol {self.tol_curve:.1e}, arc {max(self.tol_curve, 4.0*R.sag):.1e})")
                if cv is None:
                    if strict_hole:
                        return False, "bordo del foro non e' un cerchio"
                    if not self.allow_polyline:
                        return False, "bordo poligonale non convertibile"
                    if len(ch.edges) == 1:
                        n_reused += 1                # resta lo spigolo della mesh
                    else:
                        n_poly += 1
                    continue
                if ch.closed and cv.period is None:
                    n_poly += 1
                    continue
                j1, j2 = ch.verts[0], ch.verts[-1]
                V1, V2 = vertex_obj(j1), vertex_obj(j2)
                for j in (j1, j2):
                    bump_vertex(j, float(np.abs(cv.dist(topo.vpos[j][None, :]))[0]))
                Pmid = topo.vpos[ch.verts[len(ch.verts) // 2]] if len(ch.verts) > 2 else None
                Pnext = topo.vpos[ch.verts[1]] if len(ch.verts) > 1 else None
                e, fwd_is_v1 = make_edge_on_curve(cv, V1, V2, topo.vpos[j1], topo.vpos[j2],
                                                  Pmid, ch.closed, Pnext)
                if e is None:
                    self._restore_vertices(vtol_backup)
                    return False, f"MakeEdge fallita su {cv.label()}"
                e = td_Edge(e)
                # ⚠️ se il vicino e' una faccia analitica CURVA gia' convertita,
                # il nuovo spigolo ha bisogno della pcurve anche sulla SUA
                # superficie, e ce l'ha da SUBITO: BRepCheck sulla faccia vicina
                # ricostruita, senza pcurve, risponde "UnorientableShape".
                if nbp is not None and nbp.kind != PLANE:
                    why = self._pcurve_on_neighbor(e, ch.nb, nbp)
                    if why:
                        self._restore_vertices(vtol_backup)
                        return False, why
                new_edges[id(ch)] = (e, fwd_is_v1)
                replaced.append((ch, e, fwd_is_v1))
                n_analytic += 1

        if self.verbose:
            Log.debug(f"    BORDO {R.label()[:34]}: {len_tot:.2f} mm, di cui {len_debris:.2f} mm "
                      f"({100.0*len_debris/max(len_tot,1e-9):.0f}%) contro faccette tassellate")
        # --- seam per le regioni chiuse ---------------------------------------------
        seam = None
        if not R.closed_u:
            self._seam_path = None
        if R.closed_u:
            seam, why = self._make_seam(R, sp, loops, new_edges, bump_vertex)
            if seam is None:
                self._restore_vertices(vtol_backup)
                return False, why

        # --- wire e faccia ---------------------------------------------------------
        bb = BRep_Builder()
        F = TopoDS_Face()
        bb.MakeFace(F, surf, 1e-7)
        # verso dei wire: antiorario rispetto alla normale NATURALE della
        # superficie. La percorrenza della mesh e' antioraria rispetto alla
        # normale USCENTE: se la superficie e' concava (foro) va invertita.
        flip = R.concave
        self._etol_backup = {}
        wires = self._build_wires(F, sp, loops, new_edges, seam, flip)
        if isinstance(wires, str):
            self._restore_all(vtol_backup)
            return False, wires
        for w, _ in wires:
            bb.Add(F, w)
        worst_edge_tol = max(t for _, t in wires) if wires else 0.0
        if worst_edge_tol > self.max_edge_tol:
            self._restore_all(vtol_backup)
            return False, (f"tolleranza degli spigoli poligonali {worst_edge_tol:.1e} > "
                           f"tetto {self.max_edge_tol:.1e}")

        det = check_detail(F)
        if det:
            if self.verbose:
                self._dump_face(F)
            self._restore_all(vtol_backup)
            return False, "faccia non valida: " + ", ".join(det)
        mt = max_tolerance(F)
        if mt > self.max_edge_tol:
            self._restore_all(vtol_backup)
            return False, f"tolleranza {mt:.1e} sulla faccia nuova oltre il tetto {self.max_edge_tol:.1e}"
        a_mesh = float(sum(topo.areas[i] for i in rf))
        a_new = face_area(F)
        perim = sum(edge_length(topo.edges[k]) for L in loops for ch in L.chains for k in ch.edges)
        if abs(a_new - a_mesh) > 0.03 * a_mesh + 2.0 * R.sag * perim + 1e-9:
            self._restore_all(vtol_backup)
            return False, f"area incoerente: {a_new:.4f} contro {a_mesh:.4f} mm2 della mesh"

        # --- sostituzione nel solido -----------------------------------------------
        # wire dei vicini PRIMA della sostituzione: un vicino che dopo ne ha di
        # piu' ha un anello spurio (spigoli vecchi rimasti + arco nuovo = tacca)
        self._nb_wires = {}
        for ch, _, _ in replaced:
            if ch.nb not in self._nb_wires:
                self._nb_wires[ch.nb] = (topo.faces[ch.nb], count_sub(topo.faces[ch.nb], TopAbs_WIRE))
        for attempt in range(2):
            Fo = F if attempt == 0 else td_Face(F.Reversed())
            rs = BRepTools_ReShape()
            # chiavi sempre FORWARD: ReShape compone l'orientamento del nuovo
            # con quello RELATIVO fra la chiave e l'occorrenza nel solido
            rs.Replace(td_Face(topo.faces[rf[0]].Oriented(TopAbs_FORWARD)), Fo)
            for i in rf[1:]:
                rs.Remove(td_Face(topo.faces[i].Oriented(TopAbs_FORWARD)))
            for ch, e, fwd_is_v1 in replaced:
                first = topo.edges[ch.edges[0]]
                # e FORWARD va da V1 a V2 se fwd_is_v1; la catena percorre first
                # nel verso ch.fwd[0]. L'orientamento del nuovo spigolo relativo
                # al vecchio: uguale se entrambi vanno nello stesso verso.
                same = (fwd_is_v1 == ch.fwd[0])
                rs.Replace(first, e if same else td_Edge(e.Reversed()))
                for k in ch.edges[1:]:
                    rs.Remove(topo.edges[k])
            try:
                new_shape = rs.Apply(self.shape)
            except Exception as ex:
                self._restore_all(vtol_backup)
                return False, f"ReShape: {type(ex).__name__}: {ex}"
            ok, why = self._validate(new_shape, Fo, rf, replaced, R, a_mesh)
            if self.verbose:
                Log.debug(f"    tentativo {attempt+1} (Fo {Fo.Orientation()}): "
                          f"{'ok' if ok else why}")
            if ok:
                break
            # ⚠️ quando NESSUN bordo e' stato sostituito (tutte le catene
            # poligonali) il controllo di orientamento non ha spigoli nuovi da
            # guardare e non dice niente: l'errore si manifesta solo sul
            # VOLUME. Anche in quel caso va provata la faccia rovesciata.
            if attempt == 1 or ("orient" not in why and "volume" not in why):
                self._restore_all(vtol_backup)
                return False, why
        # --- accettata ------------------------------------------------------------
        self._carry_registry(rs, new_shape)
        self._register(Fo, prim)
        self.shape = new_shape
        self.topo = Topo(new_shape, prev=self.topo)
        R.note = f"{n_analytic} spigoli analitici · {n_reused} riusati · {n_poly} poligonali"
        return True, ""

    def _seam_path_search(self, targets_a: set, targets_b: set, sp: SurfParam, r_eff: float):
        """
        Cammino di spigoli INTERNI alla regione (entrambe le facce nella
        regione) da un vertice del primo anello a uno del secondo, il piu'
        "verticale" possibile (costo = scarto in u x raggio). Ritorna
        (A, B, [(indice spigolo, fwd), ...]) oppure None.
        """
        import heapq
        topo = self.topo
        rset = self._rset
        internal = [k for i in self._rf for k in topo.f_edges[i]
                    if len(topo.e_faces[k]) == 2 and all(f in rset for f in topo.e_faces[k])]
        internal = sorted(set(internal))
        if not internal:
            return None
        adjv: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
        for k in internal:
            a, b = topo.e_verts[k]
            adjv[a].append((b, k))
            adjv[b].append((a, k))
        best = None
        for A in targets_a:
            if A not in adjv:
                continue
            uA = float(sp.uv(topo.vpos[A][None, :])[0][0])
            dist = {A: 0.0}
            prev: Dict[int, Tuple[int, int]] = {}
            heap = [(0.0, A)]
            found = None
            while heap:
                d, v = heapq.heappop(heap)
                if d > dist.get(v, math.inf):
                    continue
                if v in targets_b and v != A:
                    found = v
                    break
                for w, k in adjv[v]:
                    uw = float(sp.uv(topo.vpos[w][None, :])[0][0])
                    du = abs(float(np.angle(np.exp(1j * (uw - uA)))))
                    nd = d + du * r_eff + 1e-6
                    if nd < dist.get(w, math.inf):
                        dist[w] = nd
                        prev[w] = (v, k)
                        heapq.heappush(heap, (nd, w))
            if found is None:
                continue
            if best is None or dist[found] < best[0]:
                edges = []
                v = found
                while v != A:
                    pv, k = prev[v]
                    edges.append((k, topo.e_verts[k][0] == pv))
                    v = pv
                edges.reverse()
                best = (dist[found], A, found, edges)
        if best is None:
            return None
        _, A, B, edges = best
        # lo scarto massimo in u lungo il cammino deve restare piccolo
        uA = float(sp.uv(topo.vpos[A][None, :])[0][0])
        for k, _ in edges:
            for j in topo.e_verts[k]:
                uj = float(sp.uv(topo.vpos[j][None, :])[0][0])
                # gli spigoli del cammino sono spigoli VERI della mesh, stanno
                # sulla superficie: uno scarto in u non e' un errore, basta che
                # il cammino non faccia il giro (quarto di giro al massimo)
                if abs(float(np.angle(np.exp(1j * (uj - uA))))) > math.pi / 4:
                    return None
        return A, B, edges

    def _edge_dev(self, k: int, prim: "Prim") -> float:
        """Quanto lo spigolo (la sua curva 3D, non solo gli estremi) si stacca
        dalla superficie della regione."""
        try:
            curve, t0, t1 = edge_curve(self.topo.edges[k])
            if curve is None:
                return 0.0
            P, _ = curve_points(curve, t0, t1, 7)
            return float(np.abs(prim.dist(P)).max())
        except Exception:
            return 0.0

    def _pinch_vertices(self, rf: List[int]) -> set:
        """Vertici toccati da piu' di due spigoli di bordo della regione."""
        topo = self.topo
        rset = set(rf)
        cnt: Dict[int, int] = defaultdict(int)
        for i in rf:
            for k in topo.f_edges[i]:
                fs = topo.e_faces[k]
                if len(fs) == 2 and sum(1 for f in fs if f in rset) == 1:
                    for j in topo.e_verts[k]:
                        cnt[j] += 1
        return {j for j, c in cnt.items() if c > 2}

    def _align_closed_chains(self, loops, sp: SurfParam) -> str:
        """
        Regione chiusa a 360 gradi: il seam andra' da un vertice A del primo
        anello a un vertice B del secondo alla STESSA u. Una catena chiusa che
        diventera' un cerchio ha UN solo vertice (quello di partenza): qui lo si
        sceglie in modo che i due anelli siano allineati, PRIMA di creare gli
        spigoli.
        """
        topo = self.topo

        def rotate(ch: Chain, j: int) -> bool:
            if not ch.closed or j not in ch.verts[:-1]:
                return False
            s = ch.verts.index(j)
            ch.edges = ch.edges[s:] + ch.edges[:s]
            ch.fwd = ch.fwd[s:] + ch.fwd[:s]
            core = ch.verts[:-1]
            core = core[s:] + core[:s]
            ch.verts = core + [core[0]]
            return True

        def candidates(L: Loop) -> List[int]:
            out = []
            for ch in L.chains:
                out.extend(ch.verts[:-1] if ch.closed else [ch.verts[0], ch.verts[-1]])
            return sorted(set(out))

        L1, L2 = loops
        # A: se il primo anello e' una catena chiusa, qualunque suo vertice va
        # bene: si prende quello che allinea meglio col secondo anello
        c1, c2 = candidates(L1), candidates(L2)
        if not c1 or not c2:
            return "anelli senza vertici"
        u1, _ = sp.uv(topo.vpos[c1])
        u2, _ = sp.uv(topo.vpos[c2])
        D = np.abs(np.angle(np.exp(1j * (u2[None, :] - u1[:, None]))))
        ia, ib = np.unravel_index(int(np.argmin(D)), D.shape)
        A, B = c1[ia], c2[ib]
        # ⚠️ FORI A PIU' FILE DI FACCETTE: i vertici dei due anelli non sono
        # allineati (scarto anche 0.1 mm) e un seam dritto non esiste. Allora il
        # seam e' un CAMMINO di spigoli della mesh interni alla regione, da A a
        # un vertice del secondo anello: spigoli veri, sulla superficie, con le
        # due pcurve calcolate come per qualsiasi altro spigolo.
        self._seam_path = None
        r_eff = max(prim_radius(sp.prim), 1e-6)
        if D[ia, ib] * r_eff > self.max_edge_tol:
            path = self._seam_path_search(set(c1), set(c2), sp, r_eff)
            if path is None:
                return f"seam: nessun vertice allineato sul secondo anello (scarto {D[ia, ib]*r_eff:.1e} mm)"
            A, B, edges = path
            self._seam_path = (A, B, edges)
        for ch in L1.chains:
            if ch.closed:
                rotate(ch, A)
        for ch in L2.chains:
            if ch.closed:
                rotate(ch, B)
        return ""

    def _dump_face(self, F) -> None:
        """Diagnostica: pcurve di ogni spigolo di ogni wire (estremi in u,v)."""
        cos_ = _st(BRep_Tool, "CurveOnSurface")
        for wi, w in enumerate(explore(F, TopAbs_WIRE)):
            Log.debug(f"    wire {wi} orient {w.Orientation()} closed3d={_st(BRep_Tool, 'IsClosed')(w) if hasattr(BRep_Tool, 'IsClosed_s') else '?'}")
            ex = TopExp_Explorer(w, TopAbs_EDGE)
            while ex.More():
                e = td_Edge(ex.Current())
                try:
                    c2d = cos_(e, F, 0.0, 0.0)
                    t0, t1 = bt_Range(e)
                    a, b = c2d.Value(t0), c2d.Value(t1)
                    Pa, Pb = edge_endpoints(e)
                    Log.debug(f"      edge {e.Orientation()} tol={bt_Tolerance(e):.1e} "
                              f"uv({a.X():+.4f},{a.Y():+.4f})->({b.X():+.4f},{b.Y():+.4f}) "
                              f"3d {np.round(Pa,3)}->{np.round(Pb,3)}")
                except Exception as ex_:
                    Log.debug(f"      edge {e.Orientation()}: pcurve assente ({ex_})")
                ex.Next()

    def _restore_all(self, vbackup: Dict[int, float]) -> None:
        """Rollback delle tolleranze gonfiate sul posto (vertici e spigoli condivisi)."""
        self._restore_vertices(vbackup)
        for e, t in getattr(self, "_etol_backup", {}).values():
            set_tolerance(e, t)
        self._etol_backup = {}

    def _restore_vertices(self, backup: Dict[int, float]) -> None:
        for j, t in backup.items():
            set_tolerance(td_Vertex(self.topo.vmap.FindKey(j + 1)), t)

    # --- seam ------------------------------------------------------------------
    def _make_seam(self, R: Region, sp: SurfParam, loops, new_edges, bump_vertex):
        """
        Regione chiusa a 360 gradi: serve uno spigolo di seam da un vertice A del
        primo anello a un vertice B del secondo, alla STESSA u.
        """
        topo = self.topo

        def surviving_vertices(L: Loop) -> List[int]:
            out = []
            for ch in L.chains:
                if id(ch) in new_edges:
                    out.append(ch.verts[0])
                else:
                    out.extend(ch.verts[:-1])
            return out

        L1, L2 = loops
        path = getattr(self, "_seam_path", None)
        if path is not None:
            A, B, edges = path
            seam_edges = [(topo.edges[k], fwd) for k, fwd in edges]
            return (seam_edges, A, B, True), ""
        v1 = surviving_vertices(L1)
        v2 = surviving_vertices(L2)
        if not v1 or not v2:
            return None, "anelli senza vertici"
        # A: il primo vertice sopravvissuto di L1 (se L1 e' un cerchio chiuso e'
        # obbligato); B: quello di L2 con la u piu' vicina
        u1, _ = sp.uv(topo.vpos[v1])
        u2, _ = sp.uv(topo.vpos[v2])
        best = None
        for ia, ja in enumerate(v1):
            du = np.abs(np.angle(np.exp(1j * (u2 - u1[ia]))))
            ib = int(np.argmin(du))
            if best is None or du[ib] < best[0]:
                best = (float(du[ib]), ja, v2[ib])
            if id(L1.chains[0]) in new_edges and L1.chains[0].closed:
                break                        # A e' obbligato
        du, A, B = best
        r_eff = prim_radius(R.prim)
        if du * r_eff > self.max_edge_tol:
            return None, f"seam: nessun vertice allineato sul secondo anello (scarto {du*r_eff:.1e} mm)"
        uA = float(sp.uv(topo.vpos[A][None, :])[0][0])
        vA = float(sp.uv(topo.vpos[A][None, :])[1][0])
        vB = float(sp.uv(topo.vpos[B][None, :])[1][0])
        if sp.periodic_v:
            vB = vA + float(np.angle(np.exp(1j * (vB - vA))))
        curve = sp.iso_u_curve(uA)
        if curve is None:
            return None, "seam non disponibile per questa superficie"
        curve = _keep(curve)
        # distanze reali dei vertici dalla curva del seam
        for j, t in ((A, vA), (B, vB)):
            q = curve.Value(t)
            bump_vertex(j, float(np.linalg.norm(topo.vpos[j] - np.array([q.X(), q.Y(), q.Z()]))))
        VA = td_Vertex(topo.vmap.FindKey(A + 1))
        VB = td_Vertex(topo.vmap.FindKey(B + 1))
        if vB > vA:
            me = _keep(BRepBuilderAPI_MakeEdge(curve, VA, VB, vA, vB))
            fwd_from_A = True
        else:
            me = _keep(BRepBuilderAPI_MakeEdge(curve, VB, VA, vB, vA))
            fwd_from_A = False
        if not me.IsDone():
            return None, "MakeEdge del seam fallita"
        return ([(td_Edge(me.Edge()), fwd_from_A)], A, B, True), ""

    # --- wire ------------------------------------------------------------------
    def _build_wires(self, F, sp: SurfParam, loops, new_edges, seam, flip: bool):
        """
        Costruisce i wire della faccia con le pcurve svolte lungo la percorrenza.
        Ritorna [(wire, tolleranza max degli spigoli)] oppure una stringa d'errore.
        """
        topo = self.topo
        bb = BRep_Builder()

        def chain_items(ch: Chain):
            """[(edge, fwd, is_new)] nel verso di percorrenza della catena."""
            if id(ch) in new_edges:
                e, fwd_is_v1 = new_edges[id(ch)]
                return [(e, fwd_is_v1, True)]
            return [(topo.edges[k], f, False) for k, f in zip(ch.edges, ch.fwd)]

        def loop_items(L: Loop, start_vertex: Optional[int] = None):
            items = []
            for ch in L.chains:
                items.extend(chain_items(ch))
            if start_vertex is not None:
                # ruota perche' il primo spigolo parta da start_vertex
                for s in range(len(items)):
                    e, f, _ = items[s]
                    v0 = te_FirstVertex(td_Edge(e)) if f else te_LastVertex(td_Edge(e))
                    if topo.vmap.FindIndex(v0) - 1 == start_vertex or \
                            v0.IsSame(topo.vmap.FindKey(start_vertex + 1)):
                        items = items[s:] + items[:s]
                        break
                else:
                    return None
            return items

        sequences = []
        if seam is None:
            for L in loops:
                it = loop_items(L)
                sequences.append(it)
        else:
            seam_edges, A, B, _ = seam
            L1, L2 = loops
            it1 = loop_items(L1, A)
            it2 = loop_items(L2, B)
            if it1 is None or it2 is None:
                return "seam: vertice di partenza non trovato sull'anello"
            # [seam A->B, anello 2 da B a B, seam B->A, anello 1 da A ad A]
            up = [(e, f, ("seam", k)) for k, (e, f) in enumerate(seam_edges)]
            down = [(e, not f, ("seam", k)) for k, (e, f) in reversed(list(enumerate(seam_edges)))]
            seq = up + it2 + down + it1
            sequences.append(seq)

        out = []
        for seq in sequences:
            if flip:
                seq = [(e, not f, tag) for e, f, tag in reversed(seq)]
            w = TopoDS_Wire()
            bb.MakeWire(w)
            u_prev = v_prev = None
            u_start = v_start = None
            worst = 0.0
            seam_pc = {}
            for e, fwd, tag in seq:
                e = td_Edge(e)
                curve, t0, t1 = edge_curve(e)
                if curve is None:
                    return "spigolo senza curva 3D"
                c2d, (u_end, v_end), dev = make_pcurve(sp, curve, t0, t1, fwd, u_prev, v_prev)
                if c2d is None:
                    return "pcurve non calcolabile"
                if u_start is None:
                    uu, vv = sp.uv(np.array([[curve.Value(t0 if fwd else t1).X(),
                                              curve.Value(t0 if fwd else t1).Y(),
                                              curve.Value(t0 if fwd else t1).Z()]]))
                    u_start, v_start = float(c2d.Value(t0 if fwd else t1).X()), \
                        float(c2d.Value(t0 if fwd else t1).Y())
                u_prev, v_prev = u_end, v_end
                t_old = float(bt_Tolerance(e))
                tol_e = max(t_old, 1.2 * dev + 1e-7)
                key = self.topo.emap.FindIndex(e)
                if key > 0 and key not in self._etol_backup:
                    self._etol_backup[key] = (e, t_old)       # spigolo condiviso: rollback
                if isinstance(tag, tuple) and tag[0] == "seam":
                    # ogni spigolo del seam compare due volte: una FORWARD e una
                    # REVERSED. La prima pcurve di UpdateEdge(E, C1, C2, F) e'
                    # quella dell'uso FORWARD, la seconda quella dell'uso REVERSED.
                    kk = tag[1]
                    seam_pc[(kk, bool(fwd))] = (c2d, tol_e)
                    if (kk, True) in seam_pc and (kk, False) in seam_pc:
                        c_f, t_f = seam_pc[(kk, True)]
                        c_r, t_r = seam_pc[(kk, False)]
                        bb.UpdateEdge(e, c_f, c_r, F, max(t_f, t_r))
                        worst = max(worst, t_f, t_r)
                else:
                    bb.UpdateEdge(e, c2d, F, tol_e)
                    if self.verbose and tol_e > self.max_edge_tol:
                        Log.debug(f"      spigolo oltre il tetto: tol {tol_e:.2e} "
                                  f"(scarto pcurve {dev:.2e}, tolleranza preesistente "
                                  f"{t_old:.2e}, lunghezza {edge_length(e):.4f}, "
                                  f"{'analitico' if edge_is_analytic(e) else 'polilinea'})")
                    worst = max(worst, tol_e)
                bb.Add(w, e if fwd else td_Edge(e.Reversed()))
            # chiusura nello spazio (u,v)
            if u_start is not None and u_prev is not None:
                gap = math.hypot(u_prev - u_start, v_prev - v_start)
                # scarto in parametri: i vertici stanno sulla superficie a meno
                # del rumore, quindi un piccolo scarto e' normale e lo copre la
                # tolleranza; uno scarto di ~2pi e' un errore di svolgimento
                r_eff = max(prim_radius(sp.prim), 1e-3)
                if gap * r_eff > 4.0 * self.max_edge_tol:
                    return f"wire non chiuso nei parametri (scarto {gap:.1e})"
            out.append((w, worst))
        return out

    # --- validazione sul solido ---------------------------------------------------
    def _validate(self, new_shape, Fo, rf, replaced, R: Region, a_mesh: float):
        fe = count_free_edges(new_shape)
        if fe != self.free0:
            return False, f"spigoli liberi {fe} (prima {self.free0})"
        emap = edge_face_map(new_shape)
        # orientamento coerente: ogni spigolo nuovo va percorso in versi opposti
        # dalle due facce che lo condividono
        check_edges = [e for _, e, _ in replaced]
        for e in check_edges:
            if not emap.Contains(e):
                return False, "spigolo nuovo assente dal solido"
            faces = list(_iter_list(emap.FindFromKey(e)))
            ors = []
            for f in faces:
                ors.extend(composed_edge_orientations(f, e))
            if len(ors) == 2 and ors[0] == ors[1]:
                if self.verbose:
                    Log.debug(f"    spigolo nuovo: facce {[str(f.Orientation()) for f in faces]} "
                              f"orientamenti spigolo {[str(o) for o in ors]} "
                              f"Fo={Fo.Orientation()} in faces={[f.IsSame(Fo) for f in faces]}")
                return False, "orientamento della faccia nuova incoerente coi vicini"
        # facce vicine ricostruite valide e senza anelli spuri
        touched = set()
        for (ch, e, _) in replaced:
            for f in _iter_list(emap.FindFromKey(e)):
                if not f.IsSame(Fo):
                    touched.add(self.registry.Add(f))
                    rec = self._nb_wires.get(ch.nb)
                    if rec is not None and count_sub(f, TopAbs_WIRE) > rec[1]:
                        return False, "faccia vicina con anello spurio (tacca)"
        for k in touched:
            f = self.registry.FindKey(k)
            det = check_detail(f)
            if det:
                if self.verbose:
                    Log.debug(f"    vicino non valido: tipo {face_surface_type(f)} "
                              f"area {face_area(f):.4f} -> {det}")
                    self._dump_face(td_Face(f))
                return False, "faccia vicina non valida: " + ", ".join(det)
        # anche i vicini delle catene poligonali riusate condividono spigoli con
        # tolleranza aggiornata: la faccia nuova stessa e' gia' stata validata
        # volume
        v1 = shape_volume(new_shape)
        v0 = self.vol0 if self.vol0 else shape_volume(self.shape)
        bound = 10.0 * a_mesh * max(R.sag, self.tol_fit) + 1e-9 * abs(v0) + 2e-3
        if abs(v1 - v0) > bound:
            if self.verbose:
                Log.debug(f"    volume: faccia nuova area {face_area(Fo):.4f} (mesh {a_mesh:.4f}); "
                          f"vicini toccati: " + ", ".join(
                              f"{face_surface_type(self.registry.FindKey(k))!s:.12} "
                              f"{face_area(self.registry.FindKey(k)):.4f}" for k in touched))
                for ch, e, _ in replaced:
                    Log.debug(f"      catena -> vicino area prima {self.topo.areas[ch.nb]:.4f} "
                              f"nverts {self.topo.nverts[ch.nb]} spigoli {len(ch.edges)} "
                              f"chiusa={ch.closed} lung. nuovo spigolo {edge_length(e):.4f}")
            return False, f"volume variato di {v1-v0:+.4f} mm3 (limite {bound:.4f})"
        self.vol0 = v1
        self.free0 = fe
        return True, ""

    def _pcurve_on_neighbor(self, e, nb: int, prim: "Prim") -> str:
        """Pcurve dello spigolo nuovo sulla faccia vicina analitica curva (sul posto)."""
        f = self.topo.faces[nb]
        try:
            spn = SurfParam(prim, self._frame_ref_of_face(f, prim), self._pole_axis_of_face(f))
            curve, t0, t1 = edge_curve(e)
            c2d, _, dev = make_pcurve(spn, curve, t0, t1, True, None, None)
            if c2d is None:
                return "pcurve sul vicino non calcolabile"
            tol = max(float(bt_Tolerance(e)), 1.2 * dev + 1e-7)
            if tol > self.max_edge_tol:
                return f"pcurve sul vicino: scarto {dev:.1e} oltre il tetto"
            BRep_Builder().UpdateEdge(e, c2d, f, tol)
        except Exception as ex:
            return f"pcurve sul vicino: {type(ex).__name__}: {ex}"
        return ""

    def _pole_axis_of_face(self, f):
        ad = BRepAdaptor_Surface(f, True)
        try:
            if ad.GetType() == GeomAbs_Sphere:
                d = ad.Sphere().Position().Direction()
                return np.array([d.X(), d.Y(), d.Z()])
        except Exception:
            pass
        return None

    def _frame_ref_of_face(self, f, prim):
        """Cornice della superficie gia' costruita: X dalla Geom_Surface della faccia."""
        ad = BRepAdaptor_Surface(f, True)
        t = ad.GetType()
        try:
            if t == GeomAbs_Cylinder:
                pos = ad.Cylinder().Position()
            elif t == GeomAbs_Cone:
                pos = ad.Cone().Position()
            elif t == GeomAbs_Sphere:
                pos = ad.Sphere().Position()
            elif t == GeomAbs_Torus:
                pos = ad.Torus().Position()
            else:
                return None
            d = pos.XDirection()
            x = np.array([d.X(), d.Y(), d.Z()])
            # l'asse della primitiva potrebbe essere opposto a quello della
            # superficie costruita: SurfParam ricava Y = Z x X, quindi basta X
            # e un asse coerente. Si allinea l'asse della prim a quello della faccia.
            dz = pos.Direction()
            z = np.array([dz.X(), dz.Y(), dz.Z()])
            if prim.kind != SPHERE and prim.axis is not None and float(prim.axis @ z) < 0:
                prim.axis = -prim.axis
                if prim.kind == AXIAL:
                    prim.slope = -prim.slope
            return x
        except Exception:
            return None

    def _carry_registry(self, rs, new_shape) -> None:
        """Le facce analitiche ricostruite da ReShape mantengono la loro primitiva."""
        upd = {}
        for k, prim in list(self.analytic.items()):
            old = self.registry.FindKey(k)
            try:
                if rs.IsRecorded(old):
                    new = rs.Value(old)
                    if new is not None and not new.IsNull() and not new.IsSame(old):
                        upd[self.registry.Add(new)] = prim
            except Exception:
                pass
        self.analytic.update(upd)


# =============================================================================
# 11. FASI B e C
# =============================================================================


@dataclass
class PhaseResult:
    shape: object
    regions: List[Region] = field(default_factory=list)
    n_ok: int = 0
    n_fail: int = 0
    faces_before: int = 0
    faces_after: int = 0
    free_before: int = 0
    free_after: int = 0
    vol_before: float = 0.0
    vol_after: float = 0.0
    max_tol: float = 0.0
    seconds: float = 0.0


def worst_tolerance_entity(shape) -> str:
    worst = (0.0, "")
    for v in explore(shape, TopAbs_VERTEX):
        t = float(bt_Tolerance(td_Vertex(v)))
        if t > worst[0]:
            worst = (t, f"vertice {np.round(vpos(v), 3)}")
    for e in explore(shape, TopAbs_EDGE):
        t = float(bt_Tolerance(td_Edge(e)))
        if t > worst[0]:
            worst = (t, f"spigolo da {np.round(vpos(te_FirstVertex(td_Edge(e))), 3)} "
                        f"({str(BRepAdaptor_Curve(td_Edge(e)).GetType()).split('_')[-1]})")
    for f in explore(shape, TopAbs_FACE):
        t = float(bt_Tolerance(td_Face(f)))
        if t > worst[0]:
            worst = (t, f"faccia {str(face_surface_type(f)).split('_')[-1]} area {face_area(f):.3f}")
    return f"{worst[0]:.2e} mm su {worst[1]}"


def _print_regions(regions: List[Region], title: str) -> None:
    if not regions:
        return
    Log.info(title)
    hdr = f"   {'#':>3}  {'forma':<6} {'dimensioni':<22} {'tipo':<8} {'ang.':>5} {'facce':>11}  {'RMS':>8} {'max':>8}  esito"
    _out(hdr)
    for k, R in enumerate(regions):
        _out(f"   {k:>3}  {R.label()}  {R.rms:>8.1e} {R.max_res:>8.1e}  {R.status}"
             + (f"  [{R.note}]" if R.note and R.status == 'OK' else ""))


def run_phase(shape, which: str, tol: Optional[float], max_edge_tol: Optional[float],
              min_faces: int, allow_sphere: bool, allow_cone: bool, allow_torus: bool,
              validate: bool = False, threads: int = 1) -> PhaseResult:
    """which = 'B' (solo fori) oppure 'C' (tutte le lavorazioni curve)."""
    is_b = which == "B"
    Log.banner("FASE B — fori circolari" if is_b else
               "FASE C — raccordi, smussi, svasature, sfere, bossi")
    t_all = time.perf_counter()
    res = PhaseResult(shape=shape)
    res.faces_before = count_sub(shape, TopAbs_FACE)
    res.free_before = count_free_edges(shape)
    res.vol_before = shape_volume(shape)

    topo = Topo(shape)
    diag = float(np.linalg.norm(topo.vpos.max(axis=0) - topo.vpos.min(axis=0)))
    tol_fit = tol if (tol and tol > 0) else max(2e-4, 1e-5 * diag)
    tol_grow = min(10.0 * tol_fit, 1e-3 * diag)
    if max_edge_tol is None or max_edge_tol <= 0:
        max_edge_tol = max(20.0 * tol_fit, 2e-4 * diag)
    Log.info(f"Facce {topo.nF:,} · diagonale {diag:.1f} mm · tolleranza vertici-superficie "
             f"{tol_fit:.1e} mm · tetto tolleranza spigoli {max_edge_tol:.1e} mm")

    t0 = time.perf_counter()
    regions = segment_curved(topo, tol_fit, tol_grow, diag, min_faces=min_faces,
                             allow_sphere=allow_sphere, allow_cone=allow_cone,
                             allow_torus=allow_torus, only_cyl=is_b, threads=threads)
    for R in regions:
        R.fobjs = [topo.faces[i] for i in R.faces]
    if is_b:
        regions = [R for R in regions if R.closed_u and R.concave]
        Log.info(f"Fori candidati (cilindri chiusi a 360 gradi, concavi): {len(regions)}")
    Log.debug(f"Segmentazione in {time.perf_counter()-t0:.2f}s")
    if not regions:
        Log.warn("Nessuna regione da convertire.")
        res.shape = shape
        res.faces_after = res.faces_before
        res.free_after = res.free_before
        res.vol_after = res.vol_before
        res.max_tol = max_tolerance(shape)
        res.seconds = time.perf_counter() - t_all
        return res

    eng = Engine(shape, tol_fit, diag, max_edge_tol, allow_polyline=not is_b,
                 verbose=(Log.level <= 10))
    # ordine: prima le regioni grandi (piu' bordo analitico per le successive)
    order = sorted(range(len(regions)), key=lambda k: -sum(topo.areas[i] for i in regions[k].faces))
    pending = list(order)
    # ⚠️ due passate: una regione scartata solo perche' un vicino era ancora
    # tassellato puo' riuscire dopo che il vicino e' stato convertito.
    for round_ in range(2):
        again = []
        for k in pending:
            R = regions[k]
            ok, why = eng.convert(R, strict_hole=is_b)
            if not ok and round_ == 0 and ("poligonal" in why or "tolleranza" in why or "seam" in why):
                again.append(k)
        if not again:
            break
        pending = again
        Log.info(f"Seconda passata su {len(pending)} regioni scartate per bordi poligonali")

    res.shape = eng.shape
    res.regions = regions
    res.n_ok = sum(1 for R in regions if R.status == "OK")
    res.n_fail = len(regions) - res.n_ok
    res.faces_after = count_sub(res.shape, TopAbs_FACE)
    res.free_after = count_free_edges(res.shape)
    res.vol_after = shape_volume(res.shape)
    res.max_tol = max_tolerance(res.shape)
    res.seconds = time.perf_counter() - t_all
    if res.max_tol > max_edge_tol:
        Log.warn("Tolleranza massima oltre il tetto: " + worst_tolerance_entity(res.shape))
    _print_regions(regions, "Regioni:")
    Log.ok(f"Convertite {res.n_ok}/{len(regions)} regioni · facce {res.faces_before:,} -> "
           f"{res.faces_after:,} · spigoli liberi {res.free_before} -> {res.free_after} · "
           f"volume {res.vol_before:.3f} -> {res.vol_after:.3f} mm3 "
           f"({100*(res.vol_after-res.vol_before)/max(abs(res.vol_before),1e-9):+.4f}%) · "
           f"tolleranza max {res.max_tol:.1e} mm   [{res.seconds:.1f}s]")
    if res.free_after != res.free_before:
        Log.error("Il numero di spigoli liberi e' cambiato: NON dovrebbe succedere, "
                  "segnala il caso.")
    if validate:
        ok = is_valid(res.shape)
        (Log.ok if ok else Log.warn)(f"BRepCheck: {'OK' if ok else 'NON valida'}")
    return res


# =============================================================================
# 12. REPORT
# =============================================================================


def write_report(path: str, src: str, before, after_a, results: List[PhaseResult]) -> None:
    L = ["=" * 78, "refit.py — REPORT", "=" * 78,
         f"Sorgente : {src}", f"Data     : {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
         "--- FASE A ---------------------------------------------------------",
         f"  facce  : {before['faces']:,}  ->  {after_a['faces']:,}",
         f"  edge   : {before['edges']:,}  ->  {after_a['edges']:,}",
         f"  vertici: {before['verts']:,}  ->  {after_a['verts']:,}"]
    for name, res in results:
        L += ["", f"--- FASE {name} " + "-" * (66 - len(name)),
              f"  regioni convertite : {res.n_ok} / {len(res.regions)}",
              f"  facce              : {res.faces_before:,} -> {res.faces_after:,}",
              f"  spigoli liberi     : {res.free_before} -> {res.free_after}",
              f"  volume             : {res.vol_before:.4f} -> {res.vol_after:.4f} mm3",
              f"  tolleranza max     : {res.max_tol:.2e} mm",
              f"  tempo              : {res.seconds:.1f} s", ""]
        for k, R in enumerate(res.regions):
            L.append(f"  {k:>3}  {R.label()}  rms {R.rms:.1e}  max {R.max_res:.1e}  {R.status}"
                     + (f"  [{R.note}]" if R.note and R.status == "OK" else ""))
    L.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    Log.ok(f"Report: {path}")


# =============================================================================
# 13. CLI
# =============================================================================


def default_threads() -> int:
    """Due core liberi per il sistema, mai piu' di 22: oltre non si guadagna
    piu' niente perche' i semi da provare sono poche decine."""
    n = os.cpu_count() or 2
    return max(1, min(22, n - 2))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="refit.py",
        description=(
            "Riconversione morbida di mesh (STL, o STEP nato da mesh) in B-Rep analitica.\n"
            "\n"
            "  Fase A (-a)  unione delle facce complanari e degli spigoli collineari.\n"
            "  Fase B (-b)  fori circolari passanti o ciechi: cilindro chiuso a 360 gradi,\n"
            "               concavo, che sbocca su due piani ortogonali all'asse con due\n"
            "               bordi circolari. Solo quelli: e' la fase prudente.\n"
            "  Fase C (-c)  raccordi, smussi, svasature, lamature, sfere d'angolo, bossi:\n"
            "               cilindri, coni, sfere e tori anche parziali, coi bordi lasciati\n"
            "               poligonali dove non esiste una curva esatta.\n"
            "\n"
            "Ogni regione viene sostituita da sola e subito verificata (solido ancora\n"
            "chiuso, facce valide, orientamento coerente, area e volume coerenti con la\n"
            "mesh): se un controllo non passa si torna indietro e quel pezzo resta\n"
            "tassellato. L'uscita e' sempre coerente con l'ingresso, al massimo e' meno\n"
            "pulita. Senza -a/-b/-c girano tutte e tre le fasi."),
        epilog=(
            "esempi\n"
            "  python refit.py pezzo.stl                 tutte le fasi\n"
            "  python refit.py pezzo.stl -a              solo unione delle facce complanari\n"
            "  python refit.py pezzo.stl -a 0.01         idem, unendo fino a 10 micron\n"
            "  python refit.py pezzo.stl -b -c --report  fori e lavorazioni, con report .txt\n"
            "  python refit.py pezzo.stl -b -c -a 0.01   riunione finale a 10 micron\n"
            "  python refit.py pezzo.stl -b -c -i 2      due cicli B/C prima di salvare\n"
            "  python refit.py pezzo.step -o out.step    ingresso STEP\n"
            "  python refit.py pezzo.stl -j 1            tutto in sequenza (riproducibile)\n"
            "\n"
            "note del report (--report), una riga per regione\n"
            "  [N analitici . N riusati . N poligonali]  bordi rifatti con la curva esatta;\n"
            "      bordi gia' buoni presi dalla mesh; bordi lasciati come spezzata.\n"
            "  bordi coi piani lasciati poligonali   la faccia e' esatta, i bordi coi piani\n"
            "      vicini restano quelli della mesh (secondo tentativo).\n"
            "  contorno lasciato poligonale          nessuno spigolo nuovo: il contorno\n"
            "      resta identico alla mesh (terzo tentativo, per non rompere un vicino\n"
            "      analitico gia' chiuso).\n"
            "  scartata: ...                         motivo per cui la regione resta\n"
            "      tassellata. 'regione troppo piccola in mezzo a faccette tassellate'\n"
            "      vuol dire primitiva fittata sul rumore di una zona di raccordo: meglio\n"
            "      lasciare la mesh e rifinire a mano.\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?", help="file .stl oppure .step/.stp")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="file STEP di uscita (default: <nome>_refit.step, oppure "
                        "<nome>_phaseA.step con la sola Fase A)")
    p.add_argument("--check", action="store_true",
                   help="verifica l'ambiente (OpenCascade, numpy, funzioni richieste) ed esci")

    g = p.add_argument_group("fasi (nessuna = tutte)")
    g.add_argument("-a", nargs="?", const=-1.0, type=float, default=None, dest="ph_a",
                   metavar="TOL",
                   help="Fase A: unione facce complanari. Valore opzionale = distanza max "
                        "dei vertici dal piano comune in mm (es. -a 0.01 unisce facce con "
                        "scarti fino a 10 micron). Senza valore: 2e-6 x diagonale. Insieme "
                        "a -b/-c il valore vale per la Fase A FINALE di riunione: quella "
                        "iniziale resta stretta, altrimenti le faccette delle superfici "
                        "curve si appiattiscono e le primitive non si riconoscono piu'")
    g.add_argument("-b", action="store_true", dest="ph_b",
                   help="Fase B: solo fori circolari passanti o ciechi (esegue anche A)")
    g.add_argument("-c", action="store_true", dest="ph_c",
                   help="Fase C: raccordi, smussi, lamature, sfere, tori (esegue anche A)")
    g.add_argument("-i", "--iterations", type=int, default=1, metavar="N",
                   help="ripeti le fasi B/C N volte sul risultato, con una Fase A fra un "
                        "ciclo e l'altro, prima di salvare (default 1)")
    g.add_argument("--keep-a", action="store_true",
                   help="salva anche l'intermedio <nome>_phaseA.step (solo con -b/-c)")

    g = p.add_argument_group("Fase A")
    g.add_argument("--lin-tol", type=float, default=None,
                   help="distanza max dei vertici dal piano comune per fondere due facce "
                        "(mm): e' la tolleranza della Fase A INIZIALE, quella che -a "
                        "cambia solo per la passata finale. Default: 2e-6 x diagonale, "
                        "minimo 1e-5")
    g.add_argument("--ang-tol", type=float, default=0.005,
                   help="tolleranza angolare per l'unione classica di STEP non triangolari (gradi)")

    g = p.add_argument_group("Fasi B e C")
    g.add_argument("--tol", type=float, default=None,
                   help="distanza max vertice-superficie per accettare una primitiva (mm). "
                        "Piu' e' grande, piu' regioni vengono riconosciute e piu' il "
                        "modello si allontana dalla mesh. Default: 1e-5 x diagonale, "
                        "minimo 2e-4")
    g.add_argument("--max-edge-tol", type=float, default=None,
                   help="tetto della tolleranza degli spigoli: un bordo poligonale lasciato "
                        "su una faccia analitica che si scosta di piu' fa scartare la "
                        "regione (mm). Default: 20 x --tol, e comunque almeno 2e-4 x "
                        "diagonale")
    g.add_argument("--min-faces", type=int, default=4,
                   help="faccette minime perche' un gruppo diventi una regione (default 4). "
                        "A parte questo la Fase C scarta le regioni sotto le 12 faccette "
                        "che hanno piu' del 60%% del bordo appoggiato ad altre faccette "
                        "tassellate: sono primitive fittate sul rumore")
    g.add_argument("--no-sphere", action="store_true", help="[C] non riconoscere le sfere")
    g.add_argument("--no-cone", action="store_true", help="[C] non riconoscere i coni")
    g.add_argument("--no-torus", action="store_true", help="[C] non riconoscere i tori")

    g = p.add_argument_group("sistema / output")
    g.add_argument("-j", "--threads", type=int, default=default_threads(), metavar="N",
                   help="tetto ai processi di lavoro per la ricerca delle primitive nelle "
                        f"Fasi B e C (default {default_threads()} su questa macchina, 1 = "
                        "tutto in sequenza). Quanti se ne usano davvero dipende dal lavoro "
                        "da fare. La Fase A e le sostituzioni nel solido restano su un core "
                        "solo. Con piu' processi le regioni trovate possono cambiare di "
                        "poco: con -j 1 il risultato e' riproducibile")
    g.add_argument("--validate", action="store_true",
                   help="BRepCheck completo alla fine di ogni fase (lento su shape grandi)")
    g.add_argument("--report", action="store_true",
                   help="scrivi <nome>_report.txt con l'esito di ogni regione")
    g.add_argument("--log", metavar="FILE", help="scrivi il log su file")
    g.add_argument("-v", "--verbose", action="store_true",
                   help="dettaglio per regione: curve provate, tentativi, motivi dello scarto")
    g.add_argument("--quiet", action="store_true", help="solo avvisi ed errori")
    g.add_argument("--no-color", action="store_true", help="niente colori ANSI")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    _enable_vt_windows()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    Log.no_color = args.no_color or not sys.stdout.isatty()
    Log.level = 10 if args.verbose else (30 if args.quiet else 20)

    if args.check:
        Log.banner("Verifica ambiente")
        Log.ok(f"OpenCascade  : {_NS}")
        try:
            import OCP  # noqa
            Log.ok(f"cadquery-ocp : {importlib.metadata.version('cadquery-ocp')}")
        except Exception:
            pass
        Log.ok(f"numpy        : {np.__version__}")
        Log.ok(f"python       : {sys.version.split()[0]} ({sys.platform})")
        for name, fn in (("BRep_Tool.Pnt", bt_Pnt), ("TopExp.FirstVertex", te_FirstVertex),
                         ("BRepTools_ReShape", BRepTools_ReShape),
                         ("MakeShapeOnMesh", getattr(_BRepBuilderAPI, "BRepBuilderAPI_MakeShapeOnMesh", None)),
                         ("UnifySameDomain.KeepShape", getattr(ShapeUpgrade_UnifySameDomain, "KeepShape", None))):
            (Log.ok if fn is not None else Log.error)(f"{name:<28}: {'ok' if fn is not None else 'ASSENTE'}")
        return 0

    if not args.input:
        Log.error("Manca il file di ingresso (.stl o .step). Usa -h per l'aiuto.")
        return 2

    any_flag = (args.ph_a is not None) or args.ph_b or args.ph_c
    do_b = args.ph_b or not any_flag
    do_c = args.ph_c or not any_flag
    lin_tol = args.lin_tol
    if args.ph_a is not None and args.ph_a > 0:
        lin_tol = args.ph_a
    stem, _ = os.path.splitext(args.input)
    only_a = not (do_b or do_c)
    out_path = args.output or (f"{stem}_phaseA.step" if only_a else f"{stem}_refit.step")
    t_start = time.perf_counter()

    shape, before = read_input(args.input)
    # i processi di lavoro delle Fasi B/C si accendono da subito: impiegano
    # circa un secondo a partire e lo fanno mentre gira la Fase A
    if (do_b or do_c) and args.threads > 1 and before["faces"] >= 2000:
        warm_pool(args.threads, before["faces"])

    # ⚠️ La tolleranza data con -a si applica alla Fase A FINALE (riunione) quando
    # girano anche B/C: la Fase A iniziale resta stretta, altrimenti le faccette
    # delle superfici curve si fondono in pezzi piani e le primitive non si
    # riconoscono piu'. Con la sola -a si applica subito.
    shape, before, after_a = phase_a(shape, lin_tol if only_a else args.lin_tol, args.ang_tol,
                                     validate=args.validate)
    if args.keep_a or only_a:
        write_step(shape, out_path if only_a else f"{stem}_phaseA.step")
    results: List[Tuple[str, PhaseResult]] = []
    for it in range(max(1, args.iterations)):
        tag = f" (ciclo {it+1})" if args.iterations > 1 else ""
        if do_b:
            rb = run_phase(shape, "B", args.tol, args.max_edge_tol, args.min_faces,
                           True, True, True, validate=args.validate, threads=args.threads)
            shape = rb.shape
            results.append(("B" + tag, rb))
        if do_c:
            rc = run_phase(shape, "C", args.tol, args.max_edge_tol, args.min_faces,
                           not args.no_sphere, not args.no_cone, not args.no_torus,
                           validate=args.validate, threads=args.threads)
            shape = rc.shape
            results.append(("C" + tag, rc))
        if it + 1 < args.iterations and (do_b or do_c):
            # riunione intermedia: le facce piane spezzate tornano intere e il
            # ciclo successivo riparte da una shape piu' pulita
            shape, _, _ = phase_a(shape, lin_tol, args.ang_tol,
                                  title=f"FASE A intermedia (fine ciclo {it+1})")
    if do_b or do_c:
        # ⚠️ Fase A finale: le facce piane spezzate dalle sostituzioni (e i
        # pezzetti di superfici bombate che nessuna primitiva ha preso) si
        # riuniscono con la stessa tolleranza dell'utente.
        shape, _, after_a = phase_a(shape, lin_tol, args.ang_tol, validate=args.validate,
                                    title="FASE A finale — riunione delle facce")
        Log.banner("Salvataggio")
        write_step(shape, out_path)
    if args.report:
        write_report(f"{stem}_report.txt", args.input, before, after_a, results)
    if args.log:
        Log.dump(args.log)

    Log.banner("Fatto")
    st = shape_stats(shape)
    Log.ok(f"{before['faces']:,} facce in ingresso -> {st['faces']:,} in uscita · "
           f"spigoli liberi {count_free_edges(shape)} · tolleranza max {max_tolerance(shape):.1e} mm "
           f"· {time.perf_counter()-t_start:.1f}s")
    for name, r in results:
        Log.ok(f"Fase {name}: {r.n_ok}/{len(r.regions)} regioni convertite")
    close_pool()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrotto.")
    finally:
        close_pool()
