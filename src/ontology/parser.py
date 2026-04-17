from dataclasses import dataclass, field
from pathlib import Path
from rdflib import Graph, Namespace, RDF, RDFS, OWL, XSD


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
class OntologySchema:
    domain: str
    classes: list[OntologyClass] = field(default_factory=list)
    relationships: list[OntologyRelationship] = field(default_factory=list)


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

    # Extract classes
    classes_by_uri: dict[str, OntologyClass] = {}
    for s, p, o in g.triples((None, RDF.type, OWL.Class)):
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

    return OntologySchema(
        domain=domain,
        classes=list(classes_by_uri.values()),
        relationships=relationships,
    )
