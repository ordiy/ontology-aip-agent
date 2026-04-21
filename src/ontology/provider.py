# src/ontology/provider.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from src.ontology.parser import EntityRule

@dataclass
class PhysicalMapping:
    physical_table: str
    query_engine: str = ""
    partition_keys: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class VirtualEntity:
    """A virtual/derived entity: a filtered view over a real entity.

    Attributes:
        name: Virtual entity name (dict key used elsewhere), e.g. "VIPCustomer".
        based_on: Real entity name it filters (the rdfs:label of the basedOn class).
        filter_sql: Raw SQL WHERE clause body (no 'WHERE' keyword).
    """
    name: str
    based_on: str
    filter_sql: str

@dataclass
class OntologyContext:
    schema_for_llm: str
    rules: dict[str, EntityRule]
    physical_mappings: dict[str, PhysicalMapping]
    virtual_entities: dict[str, VirtualEntity] = field(default_factory=dict)

class OntologyProvider(ABC):
    """
    解耦 LangGraph 节点与 ontology 加载实现。
    Node 只依赖此接口，不关心 RDF 文件、数据库元数据或 mock。
    """

    @abstractmethod
    def load(self) -> OntologyContext:
        """加载并解析 ontology，返回 OntologyContext。结果应被缓存。"""
        ...

    @property
    def context(self) -> OntologyContext:
        """带惰性缓存的访问入口。"""
        if not hasattr(self, "_cache"):
            self._cache = self.load()
        return self._cache
