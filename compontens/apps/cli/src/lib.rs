pub fn greet(name: &str) -> String {
    format!("hello, {name}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn greet_basic() { assert_eq!(greet("world"), "hello, world"); }
}
