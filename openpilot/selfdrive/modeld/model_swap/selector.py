import json
import threading
from pathlib import Path

from openpilot.common.params import Params
from openpilot.selfdrive.modeld.helpers import chestnut_compiled, chestnut_present

from .catalog import Bundle, bundles_from_manifest, download_bundle, fetch_manifest, model_root

STOCK_LABEL = "stock"
META_NAME = ".selected.json"
# 今の stock と同じ世代だけを選べるようにする。古い世代は入力や出力の形が違い、LegacyModelAdapter では動かない
SUPPORTED_GENERATION = 12


def model_source() -> str:
  return "chestnut" if chestnut_compiled() and chestnut_present() else "qcom"


def param_key(source: str) -> str:
  return "ModelSwapBigPkl" if source == "chestnut" else "ModelSwapPkl"


def _is_model_file(path: Path) -> bool:
  # ダウンロードしたモデル、初回読み込みで作るキャッシュ、途中で止まったダウンロードの残り
  name = path.name
  return name.endswith((".pkl", ".pkl.cache", ".part", ".tmp")) and ".pkl" in name


def cleanup_models(keep: Path | None, extra_keep=()) -> None:
  """選んでいるモデル（とそのキャッシュ）以外のモデルファイルを消す"""
  root = model_root()
  keep_names = set()
  for path in (keep, *extra_keep):
    if path is not None:
      keep_names |= {path.name, f"{path.name}.cache"}
  try:
    files = list(root.iterdir())
  except FileNotFoundError:
    return
  for path in files:
    if path.is_file() and _is_model_file(path) and path.name not in keep_names:
      try:
        path.unlink()
      except OSError:
        pass


class ModelSwapSelector:
  def __init__(self, source: str | None = None):
    self._params = Params()
    # 呼び出し側が対象を決められるようにする。ビッグモデルは chestnut が挿さって
    # いなくても選べる必要がある（Jetlink のホストが実モデルを走らせるため）
    self._source = source if source is not None else model_source()
    self._bundles: list[Bundle] | None = None
    self._loading = False
    self._busy = False
    self._error: str | None = None  # モデルの選択（ダウンロード）の失敗
    self._list_error: str | None = None  # モデル一覧の取得の失敗。起動直後の通信不可などで起き、選択には影響しない
    self._meta = self._read_meta()

  @property
  def source(self) -> str:
    return self._source

  @property
  def bundles(self) -> list[Bundle] | None:
    return self._bundles

  @property
  def loading(self) -> bool:
    return self._loading

  @property
  def busy(self) -> bool:
    return self._busy

  @property
  def error(self) -> str | None:
    return self._error

  @property
  def list_error(self) -> str | None:
    return self._list_error

  def _read_meta(self) -> dict:
    try:
      return json.loads((model_root() / META_NAME).read_text())
    except Exception:
      return {}

  def _write_meta(self, path: str, label: str) -> None:
    self._meta = {"path": path, "label": label}
    try:
      meta_path = model_root() / META_NAME
      meta_path.parent.mkdir(parents=True, exist_ok=True)
      meta_path.write_text(json.dumps(self._meta))
    except Exception:
      pass

  def _clear_meta(self) -> None:
    self._meta = {}
    try:
      (model_root() / META_NAME).unlink(missing_ok=True)
    except Exception:
      pass

  def _param_path(self) -> str | None:
    value = self._params.get(param_key(self._source))
    if isinstance(value, bytes):
      value = value.decode()
    return value or None

  def has_selection(self) -> bool:
    return self._param_path() is not None

  def current_bundle(self) -> Bundle | None:
    path = self._param_path()
    if not path:
      return None
    name = Path(path).name
    return next((bundle for bundle in self._bundles or [] if bundle.artifact.file_name == name), None)

  def has_saved_label(self) -> bool:
    path = self._param_path()
    return bool(path) and self._meta.get("path") == path and bool(self._meta.get("label"))

  def current_label(self) -> str:
    path = self._param_path()
    if not path:
      return STOCK_LABEL
    if self._meta.get("path") == path and self._meta.get("label"):
      return self._meta["label"]
    if bundle := self.current_bundle():
      return bundle.short_name
    return "selected"

  def reload(self) -> None:
    if self._loading or self._bundles is not None:
      return
    self._loading = True
    self._list_error = None

    def run() -> None:
      try:
        bundles = bundles_from_manifest(fetch_manifest(self._source))
        self._bundles = [bundle for bundle in bundles
                         if bundle.is_big == (self._source == "chestnut") and bundle.generation == SUPPORTED_GENERATION]
        if bundle := self.current_bundle():
          self._write_meta(self._param_path() or "", bundle.short_name)
      except Exception as error:
        self._list_error = str(error)
      finally:
        self._sync_jetlink()
        self._loading = False

    threading.Thread(target=run, daemon=True).start()

  def select(self, bundle: Bundle | None) -> bool:
    if self._busy:
      return False
    self._busy = True
    self._error = None
    key = param_key(self._source)

    def run() -> None:
      try:
        if bundle is None:
          own_path = self._param_path()
          self._params.remove(key)
          # メタは小/ビッグで共有なので、自分の選択を指しているときだけ消す
          if own_path and self._meta.get("path") == own_path:
            self._clear_meta()
          # stock に戻したときは消さない。すぐ同じモデルに戻せるように残しておく（次に別のモデルを選んだときに消える）
        else:
          paths = download_bundle(bundle)
          path = str(paths[0])
          self._params.put(key, path, block=True)
          self._write_meta(path, bundle.short_name)
          # 切り替えが済んでから、前に使っていたモデルを消す。ダウンロードに失敗したときは前のモデルが残る
          # 小とビッグの選択は共存させるので、両方で選んでいるファイルは残す
          selected = []
          for other in (param_key("qcom"), param_key("chestnut")):
            value = self._params.get(other)
            if isinstance(value, bytes):
              value = value.decode()
            if value:
              selected.append(Path(value))
          cleanup_models(paths[0], selected)
      except Exception as error:
        self._error = str(error)
      finally:
        self._sync_jetlink()
        self._busy = False

    threading.Thread(target=run, daemon=True).start()
    return True

  def _sync_jetlink(self) -> None:
    """Jetlink 用のカタログと選択を、現在の選択状態に合わせて更新する。

    Jetlink のホストは端末上のモデルファイルを読めないので、ビッグモデルの
    ライブラリ（chestnut のマニフェスト）と、選択中のビッグモデルの ref を
    専用パラメータに書き分ける。カタログは Jetlink のフックで拡張してから
    保存する。失敗しても選択自体は壊さない（Jetlink は既定モデルで動く）。
    """
    try:
      from openpilot import jetlink_adapter

      raw = fetch_manifest("chestnut")
      catalog = {"bundles": raw.get("bundles", [])}
      if jetlink_adapter.should_extend_catalog():
        catalog = jetlink_adapter.extend_catalog(catalog)
      self._params.put("ModelSwapJetlinkCatalog", catalog)

      pick = None
      big_path = self._params.get("ModelSwapBigPkl")
      if isinstance(big_path, bytes):
        big_path = big_path.decode()
      if big_path:
        name = Path(big_path).name
        pick = next((b for b in bundles_from_manifest(raw)
                     if b.is_big and b.artifacts and b.artifact.file_name == name), None)
      if pick is not None:
        self._params.put("ModelSwapJetlinkPick", {"ref": pick.ref, "displayName": pick.display_name}, block=True)
      else:
        self._params.remove("ModelSwapJetlinkPick")
    except Exception:
      pass
