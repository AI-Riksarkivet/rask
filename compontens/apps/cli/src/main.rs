use cli::greet;

fn main() {
    println!("{}", greet("world"));
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn main_greets() { assert_eq!(greet("you"), "hello, you"); }
}
