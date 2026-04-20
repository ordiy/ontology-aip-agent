# src/ontology/rdf_provider.py
from rdflib import Graph, Namespace, RDF, OWL, RDFS
from .provider import OntologyProvider, OntologyContext, PhysicalMapping
from .parser import parse_ontology, OntologySchema

AIP = Namespace("http://aip.example.org/rules#")


class RDFOntologyProvider(OntologyProvider):
    def __init__(self, rdf_paths: list[str]):
        self.rdf_paths = rdf_paths

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

        physical_mappings = {}
        for s in g.subjects(RDF.type, OWL.Class):
            entity_name = str(s).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for _, _, lbl in g.triples((s, RDFS.label, None)):
                entity_name = str(lbl)
                break

            physical_table = str(g.value(s, AIP.physicalTable) or "")
            query_engine = str(g.value(s, AIP.queryEngine) or "")
            partition_keys_raw = str(g.value(s, AIP.partitionKeys) or "")
            partition_keys = [k.strip() for k in partition_keys_raw.split(",") if k.strip()]

            if physical_table or query_engine or partition_keys:
                physical_mappings[entity_name] = PhysicalMapping(
                    physical_table=physical_table,
                    query_engine=query_engine,
                    partition_keys=partition_keys,
                )

        schema_for_llm = self._render_schema_for_llm(merged_schema, physical_mappings)

        return OntologyContext(
            schema_for_llm=schema_for_llm,
            rules=merged_rules,
            physical_mappings=physical_mappings,
        )

    def _render_schema_for_llm(
        self, schema: OntologySchema, physical_mappings: dict[str, PhysicalMapping]
    ) -> str:
        lines = [f"Domain: {schema.domain}", ""]

        for cls in schema.classes:
            mapping = physical_mappings.get(cls.name)
            table_name = mapping.physical_table if (mapping and mapping.physical_table) else f"{cls.name.lower()}s"

            lines.append(f"Table: {table_name}  -- entity: {cls.name}")

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

        return "\n".join(lines).strip()
