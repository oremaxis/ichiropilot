"""
jetlink on this fork: the adapter jetlink runs over, and the hooks manager,
modeld, hardwared, selfdrived and the UI call.

jetlink (the jetlink_repo submodule) runs the large driving model on an
attached Jetson, Mac or iPhone. Its stock adapter (jetlink.openpilot.stock)
knows stock openpilot's paths, and this fork's modeld and params are stock
in the ways it reads. The one difference is the model manager: model_swap
picks the models here, so the Adapter below names its big-model pick and
catalog params, and jetlink runs the picked big model instead of its own
default.

manager imports this module to build its process list, and runs it as
jetlinkd, the resident gadget owner. So the top level is the standard
library only; everything else is imported where it is used.
"""
from __future__ import annotations

import functools
import os
import threading
from collections import namedtuple
from pathlib import Path

# the version of jetlink.openpilot's API this adapter is written to; any other
# is treated as jetlink being absent, with the reason as the offroad alert
API = 3

# the gadget owner, as manager names the process and selfdrived lists it
OWNER = 'jetlinkd'

# the Jetlink setting, stored as an index: jetlink.openpilot.MODES,
# written out so the panels build the setting without a jetlink checkout
MODES = ('off', 'usb', 'ios')

# the params jetlink reads and writes, all declared in params_keys.h, with
# stock's names (jetlink.openpilot.stock.Stock.KEYS). Only the one link_on()
# reads as a file is written out here
_Keys = namedtuple('_Keys', 'link')
KEYS = _Keys(link='JetlinkLink')

_AGNOS = os.path.isfile('/AGNOS')


def _params_dir() -> Path:
  """The params store's directory, by params.cc and hw.h's rule: PARAMS_ROOT,
  else /data/params on a device and $HOME/.comma<OPENPILOT_PREFIX>/params
  elsewhere, then /<OPENPILOT_PREFIX, or d>. Stock.params_dir() without
  jetlink. Per call, so it follows a prefix as Params does; the environment
  only, so it never raises."""
  prefix = os.environ.get('OPENPILOT_PREFIX', '')
  root = os.environ.get('PARAMS_ROOT')
  if root is None:
    root = '/data/params' if _AGNOS else os.environ.get('HOME', '') + '/.comma' + prefix + '/params'
  return Path(root) / os.environ.get('OPENPILOT_PREFIX', 'd')


def link_on() -> bool:
  """Is the Jetlink setting on? As jetlink reads it, a file, so that a jetlink
  this build cannot use tells only someone who turned the link on."""
  try:
    return 0 < int((_params_dir() / KEYS.link).read_bytes()) < len(MODES)
  except (OSError, ValueError):
    return False


@functools.cache
def _adapter_class():
  from jetlink.openpilot.interface import Keys
  from jetlink.openpilot.stock import Stock

  class Adapter(Stock):
    """jetlink.openpilot.interface.Openpilot over this fork: stock openpilot,
    with model_swap's big-model pick and catalog named for jetlink."""

    # stock's keys, with the model manager's two JSON params named:
    # jetlink.openpilot.models reads the pick ({ref, displayName}) and the
    # catalog ({bundles}) from these, as it does from sunnypilot's manager
    KEYS = Keys(link='JetlinkLink', offroad='IsOffroad', progress='JetlinkProgress', spec='JetlinkSpec',
                pointers='JetlinkModelPointers', charge_phone='JetlinkChargePhone',
                big_model='ModelSwapJetlinkPick', catalog='ModelSwapJetlinkCatalog')
    # the sunnypilot-models manifests' minimum_selector_version: a catalog
    # bundle is listed only when its own carries this value
    CATALOG_SELECTOR = 21

  return Adapter


def adapter():
  """The adapter, for jetlink's entry points that run as their own process:
  the provisioning run."""
  return _adapter_class()()


def owner_config():
  """What jetlinkd needs: data only, so the owner imports nothing heavy."""
  return _adapter_class().owner_config()


def main() -> None:
  """jetlinkd: hold the USB gadget until manager stops this process."""
  _adapter_class().main()


# -- the hooks ----------------------------------------------------------------

# this process's hooks, made on first use and kept, the link off included:
# Python does not cache a failed import. One per process, so that prepare()
# and attach() reach the same jetlink
_hooks = None
_making = threading.Lock()


def hooks():
  """jetlink.openpilot.hooks.Hooks over this adapter, or the link off."""
  global _hooks
  with _making:
    if _hooks is None:
      _hooks = _guard()
    return _hooks


def _guard():
  # inside a function, so a jetlink that fails to import lands here too
  try:
    import jetlink
    if getattr(jetlink, '__file__', None) is None:
      return _Off()   # an empty jetlink_repo, which Python takes for a namespace package
    from jetlink.openpilot.hooks import Hooks
  except ModuleNotFoundError as e:
    if e.name == 'jetlink':
      return _Off()   # no checkout: the link does not exist on this device
    if (e.name or '').startswith('jetlink.'):
      return _unusable("jetlink package too old for this build", e)
    return _unusable(f"jetlink failed to load: {e}", e)
  except Exception as e:
    return _unusable(f"jetlink failed to load: {type(e).__name__}: {e}", e)
  try:
    return Hooks(API, adapter, link_on=link_on, log=_cloudlog)
  except Exception as e:
    return _unusable(str(e), e)   # another API, which it names


def _cloudlog():
  from openpilot.common.swaglog import cloudlog
  return cloudlog


def _unusable(why: str, error: Exception) -> _Off:
  try:
    _cloudlog().error("jetlink: %s", why, exc_info=error)
  except Exception:
    pass
  return _Off(why)


class _Off:
  """Every hook with the link off; reason() says why, to someone who turned
  the link on. The fork's own: jetlink's Off cannot answer for a jetlink that
  is missing or too old to have it, and one answer for every case keeps them
  alike."""
  # by hook; a callable is handed the hook's arguments. A wanted power-off goes
  # ahead: no jetlink must keep a comma on to drain the car's battery
  ANSWERS = {'should_run': False, 'status': None, 'prepare': False, 'attach': lambda model, *_: model,
             'handed_over': False, 'state': 'none', 'shutdown_step': lambda wanted, reason='': wanted,
             'runtime': None, 'model_state': None, 'swap_events': None,
             'should_extend_catalog': False, 'extend_catalog': lambda catalog: catalog}

  def __init__(self, why: str = ''):
    self.why = why

  def reason(self) -> str | None:
    return self.why if self.why and link_on() else None

  def __getattr__(self, name: str):
    if name not in self.ANSWERS:
      raise AttributeError(name)
    answer = self.ANSWERS[name]
    return answer if callable(answer) else lambda *args: answer


def _hook(name: str):
  def call(*args):
    return getattr(hooks(), name)(*args)
  call.__name__ = call.__qualname__ = name
  return call


# the hooks by the names the hook lines call, each this process's hooks() at
# the call: jetlink.openpilot.hooks says what each does and answers
HOOK_NAMES = (*_Off.ANSWERS, 'reason')
(should_run, status, prepare, attach, handed_over, state, shutdown_step, runtime, model_state,
 swap_events, should_extend_catalog, extend_catalog, reason) = map(_hook, HOOK_NAMES)
