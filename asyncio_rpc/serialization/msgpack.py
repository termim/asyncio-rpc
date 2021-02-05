import dataclasses
from pydantic import BaseModel
import msgpack
import numpy as np
from abc import ABC, abstractmethod
from datetime import datetime
from lz4.frame import compress as lz4_compress, decompress as lz4_decompress
from typing import Any

# Maximum byte lengths for str/ext
MAX_STR_LEN = 2147483647
MAX_EXT_LEN = 2147483647


# Internal registry
# TODO: figure out if it is ok to do
# this on the module...
REGISTRY = {'obj_types': {},
            'ext_types': {},
            'serializables': {},
            'pydantic_serializables': {}}


class DtypeNotSupported(Exception):
    pass


def register(obj_def):
    """
    Register dataclasses or custom handlers in the registry.

    For example obj_def and required methods, see NumpyArray below
    """
    mro = []
    if hasattr(obj_def, 'mro'):
        try:
            mro = obj_def.mro()
        except ValueError:
            pass

    if BaseModel in mro:
        # Pydantic support
        class_name = obj_def.__name__
        REGISTRY['pydantic_serializables'][class_name] = obj_def
        REGISTRY['obj_types'][obj_def] = PydanticHandler
        # Register the DataclassHandler if not done already
        if PydanticHandler.ext_type not in REGISTRY['ext_types']:
            REGISTRY['ext_types'][
                PydanticHandler.ext_type] = PydanticHandler
    elif dataclasses.is_dataclass(obj_def):
        # Handle dataclasses, every dataclass needs to be registered
        # via register.
        class_name = obj_def.__name__
        REGISTRY['serializables'][class_name] = obj_def
        REGISTRY['obj_types'][obj_def] = DataclassHandler

        # Register the DataclassHandler if not done already
        if DataclassHandler.ext_type not in REGISTRY['ext_types']:
            REGISTRY['ext_types'][
                DataclassHandler.ext_type] = DataclassHandler
    else:
        # Assume the obj_def has obj_type and ext_type, as can be
        # seen below.
        assert hasattr(obj_def, 'obj_type') and hasattr(obj_def, 'ext_type')
        REGISTRY['obj_types'][obj_def.obj_type] = obj_def
        REGISTRY['ext_types'][obj_def.ext_type] = obj_def


class AbstractHandler(ABC):
    ext_type: int = None  # Unique number
    obj_type: Any = None  # Unique object type

    @classmethod
    @abstractmethod
    def packb(cls, instance: Any) -> bytes:
        """
        Pack the instance into bytes
        """

    @classmethod
    @abstractmethod
    def unpackb(cls, data: bytes) -> Any:
        """
        Unpack the data back into an instance
        """


class NumpyArrayHandler(AbstractHandler):
    """
    Use dictionairies to serialize/deserialize
    numpy array's.

    """
    ext_type = 1
    obj_type = np.ndarray

    # Note: numpy save/load file approach
    # is a bit harder with other programming
    # languages. So use dictionaires instead.
    #
    # Note2: Currently you cannot pack/unpack
    # array's with dtype=object
    @classmethod
    def packb(cls, array: np.ndarray) -> bytes:
        if str(array.dtype) == 'object':
            raise DtypeNotSupported(
                "Numpy dtype: %s is not supported" % array.dtype)

        return dumpb({
            'shape': array.shape,
            'dtype': str(array.dtype),
            'fortran_order': np.isfortran(array),
            'data': array.tobytes()
        }, do_compress=False)

    @classmethod
    def unpackb(cls, data: bytes) -> np.ndarray:
        data = loadb(data, do_decompress=False)
        if data['dtype'] == 'object':
            raise DtypeNotSupported(
                "Numpy dtype: %s is not supported" % data['dtype'])
        res = np.frombuffer(
            data['data'], dtype=data['dtype']).reshape(
            data['shape']
        )
        if data['fortran_order']:
            res = np.asfortranarray(res)
        return res


class NumpyStructuredArrayHandler(NumpyArrayHandler):
    ext_type = 2
    obj_type = np.void  # = the type of structured array's...


class DatetimeHandler:
    """
    Serialize datetime instances as timestamps.
    """
    ext_type = 3
    obj_type = datetime

    @classmethod
    def packb(cls, dt: datetime) -> bytes:
        return b'%f' % dt.timestamp()

    @classmethod
    def unpackb(cls, data: bytes) -> datetime:
        return datetime.fromtimestamp(float(data))


class DataclassHandler:
    """
    Serialize dataclasses by serializing the .__dict__
    of dataclasses. This allows recursively serialization for example:
    dataclasses in dataclasses or Numpy array's in dataclasses.
    """
    ext_type = 4

    @classmethod
    def packb(cls, obj) -> bytes:
        dataclass_name = obj.__class__.__name__
        if isinstance(dataclass_name, str):
            dataclass_name = dataclass_name

        # Recursively process dataclasses of the dataclass,
        # serialize as tuple(dataclass_name, __dict__)
        return dumpb(
            (dataclass_name, obj.__dict__),
            do_compress=False)

    @classmethod
    def unpackb(cls, data):
        # Recursively process the contents of the dataclass
        classname, data = loadb(
            data, do_decompress=False, raw=False)
        # Return registered class or Serializable (as default)
        assert classname in REGISTRY['serializables'], \
            f'class {classname} not yet registered'
        klass = REGISTRY['serializables'][classname]
        return klass(**data)


class PydanticHandler:
    """
    Serialize pydantic models by serializing the dict
    of pydantic models.
    """
    ext_type = 6

    @classmethod
    def packb(cls, obj) -> bytes:
        dataclass_name = obj.__class__.__name__
        if isinstance(dataclass_name, str):
            dataclass_name = dataclass_name

        # Recursively process dataclasses of the dataclass,
        # serialize as tuple(dataclass_name, __dict__)
        return dumpb(
            (dataclass_name, obj.dict()),
            do_compress=False)

    @classmethod
    def unpackb(cls, data):
        # Recursively process the contents of the dataclass
        classname, data = loadb(
            data, do_decompress=False, raw=False)
        # Return registered class or Serializable (as default)
        assert classname in REGISTRY['pydantic_serializables'], \
            f'class {classname} not yet registered'
        klass = REGISTRY['pydantic_serializables'][classname]
        return klass(**data)


class SliceHandler:
    """
    Serialize slices
    """
    ext_type = 5
    obj_type = slice

    @classmethod
    def packb(cls, obj) -> bytes:
        return dumpb((obj.start, obj.stop, obj.step))

    @classmethod
    def unpackb(cls, data):
        return slice(*loadb(data))


# Register custom handlers
register(NumpyArrayHandler)
register(NumpyStructuredArrayHandler)
register(DatetimeHandler)
register(SliceHandler)


def default(obj: Any):
    """
    Serialize (dumpb) hook for obj types that msgpack does not
    process out of the box.
    """
    if type(obj) in REGISTRY['obj_types']:
        # If the type is in the registry, use the
        # handler to serialize the obj
        handler = REGISTRY['obj_types'][type(obj)]
        return msgpack.ExtType(
            handler.ext_type, handler.packb(obj))

    raise TypeError("Unknown type: %r" % (obj,))


def ext_hook(ext_type: int, bytes_data: bytes):
    """
    Deserialize (loadb) hook for ext_types that are
    not default in msgpack.

    ext_types are user defined numbers for special
    deserialization handling.
    """
    if ext_type in REGISTRY['ext_types']:
        # If the ext_type is in the registry, use the
        # handler to deserialize the bytes_data
        handler = REGISTRY['ext_types'][ext_type]
        return handler.unpackb(bytes_data)

    raise TypeError("Unknown ext_type: %r" % (ext_type,))  # pragma: no cover


def do_nothing(x):
    return x


def dumpb(instance: Any, do_compress=True, compress_func=lz4_compress,
          use_bin_type=True):
    """
    Dump/pack instance with msgpack to bytes
    """
    if not do_compress:
        compress_func = do_nothing
    return compress_func(msgpack.packb(
        instance, default=default, use_bin_type=use_bin_type))


def loadb(packed: bytes, do_decompress=True, decompress_func=lz4_decompress,
          raw=False):
    """
    Load/unpack bytes back to instance
    """
    if packed is None:
        return None
    if not do_decompress:
        decompress_func = do_nothing
    return msgpack.unpackb(
        decompress_func(packed), ext_hook=ext_hook,
        strict_map_key=False,
        max_ext_len=MAX_EXT_LEN,
        max_str_len=MAX_STR_LEN, raw=raw)
