use cli::greet;

#[test]
fn integration_greet() {
    assert_eq!(greet("integration"), "hello, integration");
}
