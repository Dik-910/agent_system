## connect_pin

### JsonConfig
```Json
{
  "briefInfo": "connect pin",
  "description": "Connects pins or ports at any level of hierarchy",
  "isAtLeastOneArg": true,
}
```
### Argument

**-from**

```Json
{
  "token": ["-from"],
  "description": "[-from from_object]  (The pin or port from which to connect)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the pin or port to connect. The direction of the pin or port cannot be inout.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
-from <*from_object*>
from_object := <list.string>
```
**-to**

```Json
{
  "token": ["-to"],
  "description": "[-to to_list]  (A list of pins and ports to which the connection must be made)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies a list of pins and ports to be connected. Pins and ports can be at any level of hierarchy. The direction of the pins or ports cannot be inout.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
-to <*to_list*>
to_list := <list.string>
```
**-port_name**

```Json
{
  "token": ["-port_name"],
  "description": "[-port_name port_name]  (The base name to use as names of ports when a new port is created on sub-designs to make the connection)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the base name to used for cross-layer connections, as the name to create a new port of the sub_module.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
[-port_name <*port_name*>]
port_name := <string>
```
**-verbose**

```Json
{
  "token": ["-verbose"],
  "description": "[-verbose]  (The tool displays individual netlist operations while making the global connections)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies that the tool displays individual netlist operations while making the global connections.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-verbose]
```
### Description

This command performs a connect between the object specified in the from_object parameter and the object specified in the to_list. The pins and ports specified in the parameter can be at any level of the hierarchy. The parent design of the pins and ports must be unique.

When connecting across layers, new ports and nets are created in the subdesign if needed. When the from pin is connected to the network, the port in the subdesign is reused, regardless of its name. In addition, if a port in the subdesign is not connected and the name is specified by the -port_name option, the port is reused.

Wherever applicable, the name of the net that is created is the name of the connecting port, as long as there is no net with the same name in that design. If there is an existing net with the name, the name of the net is generated.

connect_pin command does not allow the creation of multiple-driven nets. This command does not allow pins in the same direction to be connected.

### Example

Scenario : The following example uses connect_pin to connect U3/Y and H1/H2/U0/A.

```
connect_pin -from [get_obj_pins U3/Y] -to [get_obj_pins H1/H2/U0/A]
get_obj_connected [get_obj_nets -of [get_obj_pins U3/Y]] -leaf
```

### See Also

[connect_net](#connect_net)

[get_obj_connected](#get_obj_connected)
