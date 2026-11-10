## connect_pg_net

### JsonConfig
```Json
{
  "briefInfo": "connect pg net",
  "description": "Connects a pg net to specified pg pins or pg ports",
  "isAtLeastOneArg": true,
}
```
### Argument

**pg_net**

```Json
{
  "token": [],
  "description": "[pg_net]  (The PG net to connect)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the name or object of the pg_net to be connected.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*pg_net*>
pg_net := <string>
```
**object_list**

```Json
{
  "token": [],
  "description": "[object_list]  (A list of pg pins and ports to which the pg net is to be connected)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specified a list of pg pins and ports to which the pg net is to be connected.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
<*object_list*>
object_list := <list.string>
```
### Description

Connects a pg net to specified pg pins or pg ports.

### Example

Scenario : The following example shows how to connect the pg_net named p1 with a pg_port named p1.

```
connect_pg_net [get_obj_pg_nets p1] [get_obj_pg_ports p1]
```

### See Also

[get_obj_pg_nets](#get_obj_pg_nets)

[get_obj_pg_ports](#get_obj_pg_ports)
