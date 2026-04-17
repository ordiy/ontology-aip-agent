import pytest
from pathlib import Path

SAMPLE_RDF = """\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF
    xml:base="http://example.org/ontology/test/"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
    xmlns:ont="http://example.org/ontology/test/">

    <owl:Ontology rdf:about="http://example.org/ontology/test/">
        <rdfs:label>Test Store</rdfs:label>
    </owl:Ontology>

    <owl:Class rdf:about="http://example.org/ontology/test/Customer">
        <rdfs:label>Customer</rdfs:label>
        <rdfs:comment>A registered customer</rdfs:comment>
    </owl:Class>

    <owl:Class rdf:about="http://example.org/ontology/test/Order">
        <rdfs:label>Order</rdfs:label>
        <rdfs:comment>A purchase order</rdfs:comment>
    </owl:Class>

    <owl:Class rdf:about="http://example.org/ontology/test/Product">
        <rdfs:label>Product</rdfs:label>
        <rdfs:comment>An item for sale</rdfs:comment>
    </owl:Class>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_name">
        <rdfs:label>name</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_email">
        <rdfs:label>email</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_age">
        <rdfs:label>age</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#integer"/>
        <ont:propertyType>integer</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_orderId">
        <rdfs:label>orderId</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:isIdentifier rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</ont:isIdentifier>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_total">
        <rdfs:label>total</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <ont:propertyType>decimal</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_date">
        <rdfs:label>orderDate</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#date"/>
        <ont:propertyType>date</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/product_name">
        <rdfs:label>name</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Product"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/product_price">
        <rdfs:label>price</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Product"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <ont:propertyType>decimal</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/customer_places">
        <rdfs:label>places</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Order"/>
        <ont:cardinality>one-to-many</ont:cardinality>
    </owl:ObjectProperty>

    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/order_contains">
        <rdfs:label>contains</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Product"/>
        <ont:cardinality>many-to-many</ont:cardinality>
    </owl:ObjectProperty>

</rdf:RDF>
"""


@pytest.fixture
def sample_rdf_path(tmp_path):
    rdf_file = tmp_path / "test.rdf"
    rdf_file.write_text(SAMPLE_RDF)
    return str(rdf_file)
