//! Session-bus inputs (port of the D-Bus half of `sync.py`): player vanish
//! via `NameOwnerChanged`, and the portal's color-scheme, read once at
//! startup and then followed via `SettingChanged` (SEC-010).

use std::io;
use std::sync::mpsc::SyncSender;
use std::thread::JoinHandle;

use zbus::MatchRule;
use zbus::blocking::{Connection, MessageIterator};
use zbus::message::Type as MessageType;
use zbus::zvariant::{OwnedValue, Value};

use crate::runtime::Event;

/// Settings portal: color-scheme lives in this namespace; 0 = no preference,
/// 1 = prefer dark, 2 = prefer light.
pub const APPEARANCE_NS: &str = "org.freedesktop.appearance";
pub const SCHEME_KEY: &str = "color-scheme";
/// The theme receiver is pinned to this bus name and object path, so the
/// bus daemon drops a `SettingChanged` forged by any other peer (SEC-010).
pub const PORTAL_BUS_NAME: &str = "org.freedesktop.portal.Desktop";
pub const PORTAL_OBJECT_PATH: &str = "/org/freedesktop/portal/desktop";
pub const PORTAL_INTERFACE: &str = "org.freedesktop.portal.Settings";

/// A portal color-scheme as an integer, unwrapping the nested variant older
/// portals return from `Read`. Any integer is accepted (the mode mapping
/// treats everything but 2 as dark, as Python's `int()` then compare does);
/// a non-integer is `None`.
pub fn scheme_value(value: &Value<'_>) -> Option<u32> {
    let wide: i128 = match value {
        Value::Value(inner) => return scheme_value(inner),
        Value::U8(v) => (*v).into(),
        Value::I16(v) => (*v).into(),
        Value::U16(v) => (*v).into(),
        Value::I32(v) => (*v).into(),
        Value::U32(v) => (*v).into(),
        Value::I64(v) => (*v).into(),
        Value::U64(v) => (*v).into(),
        _ => return None,
    };
    Some(u32::try_from(wide).unwrap_or(0)) // out of range: not "light"
}

/// The event a `SettingChanged(namespace, key, value)` produces: nothing for
/// other settings, the scheme, or a rate-limited drop for a malformed value
/// (one bad signal must not disable the receiver).
pub fn scheme_event(namespace: &str, key: &str, value: &Value<'_>) -> Option<Event> {
    if namespace != APPEARANCE_NS || key != SCHEME_KEY {
        return None;
    }
    Some(match scheme_value(value) {
        Some(v) => Event::Scheme(v),
        None => Event::SchemeMalformed(format!("non-integer color-scheme value: {value}")),
    })
}

/// Read the current color-scheme from the portal, or `None` if there is no
/// portal (headless or unusual session) or it answered oddly.
pub fn read_color_scheme(conn: &Connection) -> Option<u32> {
    let reply = conn
        .call_method(
            Some(PORTAL_BUS_NAME),
            PORTAL_OBJECT_PATH,
            Some(PORTAL_INTERFACE),
            "Read",
            &(APPEARANCE_NS, SCHEME_KEY),
        )
        .ok()?;
    let value: OwnedValue = reply.body().deserialize().ok()?;
    scheme_value(&value)
}

fn spawn_listener(
    name: &str,
    conn: &Connection,
    rule: MatchRule<'static>,
    mut handle: impl FnMut(&zbus::Message) -> Option<Event> + Send + 'static,
    tx: SyncSender<Event>,
) -> io::Result<JoinHandle<()>> {
    let messages =
        MessageIterator::for_match_rule(rule, conn, Some(64)).map_err(io::Error::other)?;
    std::thread::Builder::new()
        .name(name.into())
        .spawn(move || {
            for msg in messages.flatten() {
                if let Some(event) = handle(&msg) {
                    if tx.send(event).is_err() {
                        return; // the loop is gone
                    }
                }
            }
        })
}

/// Follow the portal's `SettingChanged`, pinned to the portal's bus name and
/// object path.
pub fn watch_scheme(conn: &Connection, tx: SyncSender<Event>) -> io::Result<JoinHandle<()>> {
    let rule = MatchRule::builder()
        .msg_type(MessageType::Signal)
        .sender(PORTAL_BUS_NAME)
        .and_then(|b| b.path(PORTAL_OBJECT_PATH))
        .and_then(|b| b.interface(PORTAL_INTERFACE))
        .and_then(|b| b.member("SettingChanged"))
        .map_err(io::Error::other)?
        .build();
    spawn_listener(
        "dbus-scheme",
        conn,
        rule,
        |msg| {
            let (ns, key, value): (String, String, OwnedValue) = msg.body().deserialize().ok()?;
            scheme_event(&ns, &key, &value)
        },
        tx,
    )
}

/// Report every lost bus name: this is what reverts to the preset when the
/// last player closes (`playerctl --follow` emits no line for a vanished
/// player). The coordinator ignores names that are not tracked players.
pub fn watch_vanish(conn: &Connection, tx: SyncSender<Event>) -> io::Result<JoinHandle<()>> {
    let rule = MatchRule::builder()
        .msg_type(MessageType::Signal)
        .sender("org.freedesktop.DBus")
        .and_then(|b| b.interface("org.freedesktop.DBus"))
        .and_then(|b| b.member("NameOwnerChanged"))
        .map_err(io::Error::other)?
        .build();
    spawn_listener(
        "dbus-vanish",
        conn,
        rule,
        |msg| {
            let (name, _old, new): (String, String, String) = msg.body().deserialize().ok()?;
            new.is_empty().then_some(Event::Vanish(name))
        },
        tx,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Command, Stdio};
    use std::sync::mpsc::{Receiver, sync_channel};
    use std::time::Duration;
    use zbus::zvariant::Str;

    // --- payload handling (SchemeHandlerTest) ----------------------------

    #[test]
    fn accepts_a_valid_portal_value_in_any_integer_type() {
        for v in [Value::U32(2), Value::I32(2), Value::U8(2), Value::I64(2)] {
            assert!(matches!(
                scheme_event(APPEARANCE_NS, SCHEME_KEY, &v),
                Some(Event::Scheme(2))
            ));
        }
        let nested = Value::Value(Box::new(Value::U32(1)));
        assert_eq!(scheme_value(&nested), Some(1));
    }

    #[test]
    fn ignores_other_settings() {
        assert!(scheme_event("org.gnome.desktop.interface", SCHEME_KEY, &Value::U32(2)).is_none());
        assert!(scheme_event(APPEARANCE_NS, "accent-color", &Value::U32(2)).is_none());
    }

    #[test]
    fn malformed_values_are_reported_not_raised() {
        for v in [
            Value::Str(Str::from("dark")),
            Value::F64(2.0),
            Value::Bool(true),
        ] {
            let e = scheme_event(APPEARANCE_NS, SCHEME_KEY, &v);
            assert!(matches!(e, Some(Event::SchemeMalformed(_))), "{v:?}");
        }
    }

    #[test]
    fn out_of_range_integers_map_to_not_light() {
        assert_eq!(scheme_value(&Value::I32(-1)), Some(0));
        assert_eq!(scheme_value(&Value::U64(u64::MAX)), Some(0));
    }

    // --- against a real, private dbus-daemon ------------------------------

    /// A private session bus for the test, killed on drop.
    struct Bus {
        daemon: Child,
        address: String,
    }

    impl Bus {
        /// `None` (and the test is skipped) where dbus-daemon is missing.
        fn start() -> Option<Bus> {
            let mut daemon = Command::new("dbus-daemon")
                .args(["--session", "--nofork", "--print-address=1"])
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .ok()?;
            let mut line = String::new();
            BufReader::new(daemon.stdout.take()?)
                .read_line(&mut line)
                .ok()?;
            Some(Bus {
                daemon,
                address: line.trim().to_string(),
            })
        }

        fn connect(&self) -> Connection {
            zbus::blocking::connection::Builder::address(self.address.as_str())
                .unwrap()
                .build()
                .unwrap()
        }
    }

    impl Drop for Bus {
        fn drop(&mut self) {
            let _ = self.daemon.kill();
            let _ = self.daemon.wait();
        }
    }

    fn next(rx: &Receiver<Event>) -> Option<Event> {
        rx.recv_timeout(Duration::from_secs(2)).ok()
    }

    fn emit_setting(conn: &Connection, value: u32) {
        conn.emit_signal(
            None::<&str>,
            PORTAL_OBJECT_PATH,
            PORTAL_INTERFACE,
            "SettingChanged",
            &(APPEARANCE_NS, SCHEME_KEY, Value::U32(value)),
        )
        .unwrap();
    }

    #[test]
    fn scheme_receiver_is_pinned_to_the_portal_bus_name() {
        let Some(bus) = Bus::start() else {
            eprintln!("skipped: dbus-daemon not available");
            return;
        };
        let daemon_conn = bus.connect();
        let (tx, rx) = sync_channel(8);
        watch_scheme(&daemon_conn, tx).unwrap();

        // A peer that is not the portal: its signal must never arrive.
        let impostor = bus.connect();
        emit_setting(&impostor, 2);

        let portal = bus.connect();
        portal.request_name(PORTAL_BUS_NAME).unwrap();
        emit_setting(&portal, 1);

        assert!(
            matches!(next(&rx), Some(Event::Scheme(1))),
            "only the portal's signal"
        );
        assert!(rx.recv_timeout(Duration::from_millis(200)).is_err());
    }

    #[test]
    fn a_lost_player_name_is_reported_as_vanished() {
        let Some(bus) = Bus::start() else {
            eprintln!("skipped: dbus-daemon not available");
            return;
        };
        let daemon_conn = bus.connect();
        let (tx, rx) = sync_channel(8);
        watch_vanish(&daemon_conn, tx).unwrap();

        let player = bus.connect();
        player
            .request_name("org.mpris.MediaPlayer2.spotify")
            .unwrap();
        drop(player); // the player exits
        let mut seen = Vec::new();
        while let Some(Event::Vanish(name)) = next(&rx) {
            if name == "org.mpris.MediaPlayer2.spotify" {
                return;
            }
            seen.push(name); // the player's unique name vanishes too
        }
        panic!("no vanish for the player's name; saw {seen:?}");
    }

    #[test]
    fn startup_read_returns_none_without_a_portal() {
        let Some(bus) = Bus::start() else {
            eprintln!("skipped: dbus-daemon not available");
            return;
        };
        assert_eq!(read_color_scheme(&bus.connect()), None);
    }
}
