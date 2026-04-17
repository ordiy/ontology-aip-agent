from src.ontology.parser import parse_ontology, OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship


def test_parse_extracts_domain_name(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    assert schema.domain == "Test Store"


def test_parse_extracts_all_classes(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    class_names = {c.name for c in schema.classes}
    assert class_names == {"Customer", "Order", "Product"}


def test_parse_extracts_properties_for_class(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    customer = next(c for c in schema.classes if c.name == "Customer")
    prop_names = {p.name for p in customer.properties}
    assert prop_names == {"name", "email", "age"}


def test_parse_property_types(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    customer = next(c for c in schema.classes if c.name == "Customer")
    age_prop = next(p for p in customer.properties if p.name == "age")
    assert age_prop.data_type == "integer"
    email_prop = next(p for p in customer.properties if p.name == "email")
    assert email_prop.data_type == "string"


def test_parse_identifier_property(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    order = next(c for c in schema.classes if c.name == "Order")
    order_id_prop = next(p for p in order.properties if p.name == "orderId")
    assert order_id_prop.is_identifier is True


def test_parse_decimal_maps_to_float(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    order = next(c for c in schema.classes if c.name == "Order")
    total_prop = next(p for p in order.properties if p.name == "total")
    assert total_prop.data_type == "float"


def test_parse_extracts_relationships(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    assert len(schema.relationships) == 2
    rel_names = {r.name for r in schema.relationships}
    assert rel_names == {"places", "contains"}


def test_parse_relationship_details(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    places = next(r for r in schema.relationships if r.name == "places")
    assert places.source == "Customer"
    assert places.target == "Order"
    assert places.cardinality == "one-to-many"


def test_parse_many_to_many_relationship(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    contains = next(r for r in schema.relationships if r.name == "contains")
    assert contains.source == "Order"
    assert contains.target == "Product"
    assert contains.cardinality == "many-to-many"


def test_parse_nonexistent_file_raises(sample_rdf_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        parse_ontology("/nonexistent/file.rdf")
