import logging

from rdflib import Graph, Namespace, RDF, OWL, RDFS
from src.ontology.provider import OntologyProvider, OntologyContext, PhysicalMapping, SecurityPolicy, VirtualEntity
from src.ontology.parser import parse_ontology, OntologySchema
from src.ontology.context import table_name as _sqlite_table_name

logger = logging.getLogger(__name__)
AIP = Namespace("http://aip.example.org/rules#")

class RDFOntologyProvider(OntologyProvider):
    def __init__(self, rdf_paths: list[str], executor_dialect: str = "SQLite"):
        self.rdf_paths = rdf_paths
        self._executor_dialect = executor_dialect

    def load(self) -> OntologyContext:
        merged_classes = []
        merged_relationships = []
        merged_rules = {}
        domain = "Unknown"

        for i, path in enumerate(self.rdf_paths):
            schema = parse_ontology(path)
            if i == 0:
                domain = schema.domain
            merged_classes.extend(schema.classes)
            merged_relationships.extend(schema.relationships)
            merged_rules.update(schema.rules)

        merged_schema = OntologySchema(
            domain=domain,
            classes=merged_classes,
            relationships=merged_relationships,
            rules=merged_rules,
        )

        g = Graph()
        for path in self.rdf_paths:
            g.parse(path, format="xml")

        class_uris = list(g.subjects(RDF.type, OWL.Class))

        physical_mappings = {}
        for s in class_uris:
            entity_name = str(s).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for _, _, lbl in g.triples((s, RDFS.label, None)):
                entity_name = str(lbl)
                break

            physical_table = str(g.value(s, AIP.physicalTable) or "")
            query_engine = str(g.value(s, AIP.queryEngine) or "")
            partition_keys_raw = str(g.value(s, AIP.partitionKeys) or "")
            partition_keys = [k.strip() for k in partition_keys_raw.split(",") if k.strip()]

            # ── Security policy annotations ──────────────────────────────────
            policy: SecurityPolicy | None = None
            required_roles_raw = str(g.value(s, AIP.requiresRole) or "")
            row_filter = g.value(s, AIP.rowFilter)
            mask_columns_raw = str(g.value(s, AIP.maskColumns) or "")

            # Collect all requiresRole triples (may be multi-valued)
            role_set: frozenset[str] = frozenset(
                str(o).strip()
                for _, _, o in g.triples((s, AIP.requiresRole, None))
                if str(o).strip()
            )
            row_filter_str: str | None = str(row_filter) if row_filter else None
            masked_columns: dict[str, str] = {}
            if mask_columns_raw.strip():
                for pair in mask_columns_raw.split(","):
                    pair = pair.strip()
                    if ":" in pair:
                        col, method = pair.split(":", 1)
                        masked_columns[col.strip()] = method.strip()

            if role_set or row_filter_str or masked_columns:
                policy = SecurityPolicy(
                    required_roles=role_set,
                    row_filter_template=row_filter_str,
                    masked_columns=masked_columns,
                )

            if physical_table or query_engine or partition_keys:
                physical_mappings[entity_name] = PhysicalMapping(
                    physical_table=physical_table,
                    query_engine=query_engine,
                    partition_keys=partition_keys,
                    policy=policy,
                )

        virtual_entities = self._load_virtual_entities(g, class_uris)

        schema_for_llm = self._render_schema_for_llm(merged_schema, physical_mappings, virtual_entities)

        return OntologyContext(
            schema_for_llm=schema_for_llm,
            rules=merged_rules,
            physical_mappings=physical_mappings,
            virtual_entities=virtual_entities,
        )

    def _load_virtual_entities(self, graph: Graph, class_uris: list) -> dict[str, VirtualEntity]:
        """Extract virtual entity annotations from the RDF graph via SPARQL-like node matching."""
        virtual_entities = {}
        for s in class_uris:
            is_virtual_literal = graph.value(s, AIP.isVirtual)
            if not is_virtual_literal:
                continue
                
            is_virtual = str(is_virtual_literal).lower() == "true"
            if not is_virtual:
                continue

            entity_name = str(s).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for _, _, lbl in graph.triples((s, RDFS.label, None)):
                entity_name = str(lbl)
                break

            based_on_uri = graph.value(s, AIP.basedOn)
            filter_sql = str(graph.value(s, AIP.filter) or "")

            if not based_on_uri or not filter_sql:
                logger.warning(f"Virtual entity {entity_name} missing basedOn or filter. Skipping.")
                continue

            based_on_name = str(based_on_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for _, _, lbl in graph.triples((based_on_uri, RDFS.label, None)):
                based_on_name = str(lbl)
                break

            virtual_entities[entity_name] = VirtualEntity(
                name=entity_name,
                based_on=based_on_name,
                filter_sql=filter_sql,
            )

        return virtual_entities

    def _render_schema_for_llm(
        self, schema: OntologySchema, physical_mappings: dict[str, PhysicalMapping], virtual_entities: dict[str, VirtualEntity]
    ) -> str:
        lines = [f"Domain: {schema.domain}", ""]

        use_physical = self._executor_dialect.lower() != "sqlite"

        for cls in schema.classes:
            mapping = physical_mappings.get(cls.name)
            if use_physical and mapping and mapping.physical_table:
                tbl = mapping.physical_table          # e.g. iceberg_catalog.ecommerce.buyers
            else:
                tbl = _sqlite_table_name(cls.name)    # e.g. buyers

            lines.append(f"Table: {tbl}  -- entity: {cls.name}")

            cols = [
                f"{p.name}({p.data_type})" + (" PK" if p.is_identifier else "")
                for p in cls.properties
            ]
            lines.append(f"  Columns: {', '.join(cols)}" if cols else "  Columns: (none)")

            if mapping and mapping.partition_keys:
                lines.append(f"  Partitioned by: {', '.join(mapping.partition_keys)}")

            rule = schema.rules.get(cls.name)
            if rule and rule.decision_rule:
                lines.append(f"  [Decision Rule]: {rule.decision_rule}")

            lines.append("")
            
        for virt in virtual_entities.values():
            lines.append(f"Virtual entity: {virt.name}  -- based on: {virt.based_on}  -- filter: {virt.filter_sql}")
            
        return "\n".join(lines).strip()
