from dataclasses import dataclass, field
from pathlib import Path
from rdflib import Graph, Namespace, RDF, RDFS, OWL, XSD

AIP = Namespace("http://aip.example.org/rules#")


@dataclass
class OntologyProperty:
    name: str
    data_type: str  # string, integer, float, date, datetime, boolean
    is_identifier: bool = False


@dataclass
class OntologyClass:
    name: str
    properties: list[OntologyProperty] = field(default_factory=list)


@dataclass
class OntologyRelationship:
    source: str
    target: str
    name: str
    cardinality: str  # one-to-one, one-to-many, many-to-one, many-to-many


@dataclass
class EntityRule:
    """Pattern D: business rules extracted from aip: annotations on an owl:Class."""
    entity: str
    decision_rule: str = ""          # IF-THEN rule text
    operation_steps: list[str] = field(default_factory=list)  # ordered step names
    requires_approval: str = "user"  # auto | user | admin
    rollbackable: bool = True
    overridable: bool = True
    severity: str = "medium"         # low | medium | high | critical


@dataclass
class OntologySchema:
    domain: str
    classes: list[OntologyClass] = field(default_factory=list)
    relationships: list[OntologyRelationship] = field(default_factory=list)
    rules: dict[str, EntityRule] = field(default_factory=dict)  # entity_name → EntityRule


# Normalize ont:propertyType values to our internal types
_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "int": "integer",
    "decimal": "float",
    "float": "float",
    "double": "float",
    "date": "date",
    "datetime": "datetime",
    "dateTime": "datetime",
    "boolean": "boolean",
    "bool": "boolean",
}

# XSD URI → internal type
_XSD_MAP = {
    str(XSD.string): "string",
    str(XSD.integer): "integer",
    str(XSD.int): "integer",
    str(XSD.decimal): "float",
    str(XSD.float): "float",
    str(XSD.double): "float",
    str(XSD.date): "date",
    str(XSD.dateTime): "datetime",
    str(XSD.boolean): "boolean",
}


def _uri_local_name(uri: str) -> str:
    """Extract local name from a URI (after last # or /, skipping trailing separators)."""
    uri = uri.rstrip("/#")
    if "#" in uri:
        return uri.rsplit("#", 1)[1]
    return uri.rsplit("/", 1)[1] if "/" in uri else uri


def parse_ontology(rdf_path: str) -> OntologySchema:
    path = Path(rdf_path)
    if not path.exists():
        raise FileNotFoundError(f"RDF file not found: {rdf_path}")

    g = Graph()
    g.parse(str(path), format="xml")

    # Detect ont: namespace from the declared xmlns:ont prefix
    ont_prefix_uri = dict(g.namespaces()).get("ont")
    ont_ns = Namespace(str(ont_prefix_uri)) if ont_prefix_uri else None

    # Extract domain name from the first owl:Ontology triple that has a rdfs:label
    domain = "Unknown"
    for s, p, o in g.triples((None, RDF.type, OWL.Ontology)):
        for _, _, label in g.triples((s, RDFS.label, None)):
            domain = str(label)
            break
        if domain != "Unknown":
            break

    # Extract classes (skip virtual entities — they are views, not tables)
    classes_by_uri: dict[str, OntologyClass] = {}
    for s, p, o in g.triples((None, RDF.type, OWL.Class)):
        is_virtual = g.value(s, AIP.isVirtual)
        if is_virtual is not None and str(is_virtual).lower() == "true":
            continue
        uri = str(s)
        label = _uri_local_name(uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            label = str(lbl)
            break
        classes_by_uri[uri] = OntologyClass(name=label)

    # Extract datatype properties
    for s, p, o in g.triples((None, RDF.type, OWL.DatatypeProperty)):
        prop_uri = str(s)

        prop_name = _uri_local_name(prop_uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            prop_name = str(lbl)
            break

        domain_uri = None
        for _, _, dom in g.triples((s, RDFS.domain, None)):
            domain_uri = str(dom)
            break

        data_type = "string"
        if ont_ns:
            for _, _, pt in g.triples((s, ont_ns.propertyType, None)):
                raw = str(pt)
                data_type = _TYPE_MAP.get(raw, "string")
                break
        if data_type == "string":
            for _, _, rng in g.triples((s, RDFS.range, None)):
                range_uri = str(rng)
                data_type = _XSD_MAP.get(range_uri, "string")
                break

        is_identifier = False
        if ont_ns:
            for _, _, ident in g.triples((s, ont_ns.isIdentifier, None)):
                is_identifier = str(ident).lower() == "true"
                break

        prop = OntologyProperty(name=prop_name, data_type=data_type, is_identifier=is_identifier)

        if domain_uri and domain_uri in classes_by_uri:
            classes_by_uri[domain_uri].properties.append(prop)

    # Extract object properties (relationships)
    relationships = []
    for s, p, o in g.triples((None, RDF.type, OWL.ObjectProperty)):
        rel_uri = str(s)

        rel_name = _uri_local_name(rel_uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            rel_name = str(lbl)
            break

        source_uri = None
        for _, _, dom in g.triples((s, RDFS.domain, None)):
            source_uri = str(dom)
            break

        target_uri = None
        for _, _, rng in g.triples((s, RDFS.range, None)):
            target_uri = str(rng)
            break

        cardinality = "one-to-many"
        if ont_ns:
            for _, _, card in g.triples((s, ont_ns.cardinality, None)):
                cardinality = str(card)
                break

        if source_uri and target_uri:
            source_name = classes_by_uri[source_uri].name if source_uri in classes_by_uri else _uri_local_name(source_uri)
            target_name = classes_by_uri[target_uri].name if target_uri in classes_by_uri else _uri_local_name(target_uri)
            relationships.append(OntologyRelationship(
                source=source_name,
                target=target_name,
                name=rel_name,
                cardinality=cardinality,
            ))

    # Extract aip: rules from owl:Class annotations
    rules: dict[str, EntityRule] = {}
    for cls_uri, ont_class in classes_by_uri.items():
        cls_node = next(g.subjects(RDF.type, OWL.Class), None)
        # Use the URI directly for triple lookups
        from rdflib import URIRef
        cls_ref = URIRef(cls_uri)

        decision_rule = str(g.value(cls_ref, AIP.decisionRule) or "")
        steps_raw = str(g.value(cls_ref, AIP.operationSteps) or "")
        requires_approval = str(g.value(cls_ref, AIP.requiresApproval) or "user")
        rollbackable_val = g.value(cls_ref, AIP.rollbackable)
        rollbackable = str(rollbackable_val).lower() != "false" if rollbackable_val else True
        overridable_val = g.value(cls_ref, AIP.overridable)
        overridable = str(overridable_val).lower() != "false" if overridable_val else True
        severity = str(g.value(cls_ref, AIP.severity) or "medium")

        operation_steps = _parse_operation_steps(steps_raw)

        if decision_rule or operation_steps:
            rules[ont_class.name] = EntityRule(
                entity=ont_class.name,
                decision_rule=decision_rule,
                operation_steps=operation_steps,
                requires_approval=requires_approval,
                rollbackable=rollbackable,
                overridable=overridable,
                severity=severity,
            )

    return OntologySchema(
        domain=domain,
        classes=list(classes_by_uri.values()),
        relationships=relationships,
        rules=rules,
    )


def _parse_operation_steps(steps_str: str) -> list[str]:
    """Parse '1:verify_overdue,2:notify_customer' → ['verify_overdue', 'notify_customer']."""
    if not steps_str.strip():
        return []
    result = []
    for part in steps_str.split(","):
        part = part.strip()
        if ":" in part:
            result.append(part.split(":", 1)[1].strip())
        elif part:
            result.append(part)
    return result
