//! Rust Type-State Pattern & Zero-Cost Abstraction Invariant Benchmark.
//!
//! Evaluates the enforcement of state-machine invariants at compile time:
//! 1. State transitions consume ownership (affine types), preventing use-after-free and reuse bugs.
//! 2. Zero-sized marker types guarantee zero runtime memory overhead (`size_of::<State>() == 0`).
//! 3. Invalid transitions (e.g. sending before authentication or double-connecting) are compile errors.
//!
//! Strictly enforces memory safety: `#![forbid(unsafe_code)]`.

#![forbid(unsafe_code)]

use std::marker::PhantomData;

/// Zero-sized marker for unauthenticated connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Unauthenticated;

/// Zero-sized marker for authenticated connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Authenticated;

/// Zero-sized marker for active connected state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Connected;

/// Zero-sized marker for closed connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Closed;

/// Type-state connection harness parameterized over its lifecycle state.
#[derive(Debug)]
pub struct Connection<State> {
    endpoint: String,
    token: Option<String>,
    _state: PhantomData<State>,
}

impl Connection<Unauthenticated> {
    /// Create a new connection in the Unauthenticated initial state.
    pub fn new(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            token: None,
            _state: PhantomData,
        }
    }

    /// Authenticate using an access token, consuming self and transitioning to Authenticated.
    pub fn authenticate(self, token: impl Into<String>) -> Result<Connection<Authenticated>, &'static str> {
        let tok = token.into();
        if tok.is_empty() {
            return Err("Token cannot be empty");
        }
        Ok(Connection {
            endpoint: self.endpoint,
            token: Some(tok),
            _state: PhantomData,
        })
    }
}

impl Connection<Authenticated> {
    /// Establish transport session, consuming self and transitioning to Connected.
    pub fn connect(self) -> Connection<Connected> {
        Connection {
            endpoint: self.endpoint,
            token: self.token,
            _state: PhantomData,
        }
    }
}

impl Connection<Connected> {
    /// Transmit a message payload over the active authenticated connection.
    pub fn send(&self, payload: &str) -> Result<usize, &'static str> {
        if payload.is_empty() {
            return Err("Payload cannot be empty");
        }
        Ok(payload.len())
    }

    /// Close the connection session, consuming self and transitioning to Closed.
    pub fn close(self) -> Connection<Closed> {
        Connection {
            endpoint: self.endpoint,
            token: None,
            _state: PhantomData,
        }
    }
}

impl Connection<Closed> {
    /// Check if connection is cleanly terminated.
    pub fn is_closed(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zero_sized_type_state_overhead() {
        assert_eq!(std::mem::size_of::<Unauthenticated>(), 0);
        assert_eq!(std::mem::size_of::<Authenticated>(), 0);
        assert_eq!(std::mem::size_of::<Connected>(), 0);
        assert_eq!(std::mem::size_of::<Closed>(), 0);
    }

    #[test]
    fn test_valid_lifecycle_transitions() {
        let conn = Connection::new("https://example.com/api");
        let authed = conn.authenticate("valid-secret-token").expect("auth failed");
        let connected = authed.connect();

        let bytes_sent = connected.send("hello invariant").expect("send failed");
        assert_eq!(bytes_sent, 15);

        let closed = connected.close();
        assert!(closed.is_closed());
    }

    #[test]
    fn test_empty_token_rejection() {
        let conn = Connection::new("https://example.com/api");
        let result = conn.authenticate("");
        assert!(result.is_err());
    }

    #[test]
    fn test_empty_payload_rejection() {
        let conn = Connection::new("https://example.com/api");
        let authed = conn.authenticate("valid-token").unwrap();
        let connected = authed.connect();
        assert!(connected.send("").is_err());
    }
}
