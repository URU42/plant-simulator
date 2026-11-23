package CduHydraulics
  constant Real pi = 3.141592653589793;

  connector Port
    Real p(unit = "Pa") "절대압";
    flow Real q(unit = "m3/s") "요소 안으로 흐르는 유량";
  end Port;

  model Boundary
    parameter Real p(unit = "Pa");
    Port a;
  equation
    a.p = p;
  end Boundary;

  partial model TwoPort
    Port a, b;
    Real q(unit = "m3/s");
  equation
    a.q = q;
    b.q = -q;
  end TwoPort;

  model Pipe
    extends TwoPort;
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    parameter Real length(unit = "m"), diameter(unit = "m");
    parameter Real darcy, minorK, dz(unit = "m") = 0;
    final parameter Real area(unit = "m2") = pi * diameter^2 / 4;
  equation
    a.p - b.p = rho * g * dz
      + (darcy * length / diameter + minorK) * rho / 2 * (q / area) * abs(q / area);
  end Pipe;

  model Valve
    extends TwoPort;
    parameter Real rho(unit = "kg/m3"), diameter(unit = "m");
    parameter Real fullK, opening;
    final parameter Real area(unit = "m2") = pi * diameter^2 / 4;
  equation
    a.p - b.p = fullK * rho / 2 * (q / (area * opening)) * abs(q / (area * opening));
  end Valve;

  model Strainer
    extends TwoPort;
    parameter Real resistance(unit = "Pa.s2/m6") = 0;
  equation
    a.p - b.p = resistance * q * abs(q);
  end Strainer;

  model Pump
    extends TwoPort;
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    parameter Real referenceRpm, rpm, eta;
    parameter Real ratedFlow(unit = "m3/s"), zeroHead(unit = "m"), ratedHead(unit = "m");
    Real head(unit = "m"), hydraulic(unit = "W"), shaft(unit = "W");
  equation
    head = (rpm / referenceRpm)^2 * zeroHead
      - (zeroHead - ratedHead) * (q / ratedFlow)^2;
    b.p - a.p = rho * g * head;
    hydraulic = q * (b.p - a.p);
    shaft = hydraulic / eta;
  end Pump;

  model Circuit
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    Boundary source, otherSink, productSink;
    Strainer strainer;
    Pump feed(rho = rho, g = g, q(start = 0.11));
    Pipe otherLine(rho = rho, g = g, q(start = 0.05));
    Pipe processLine(rho = rho, g = g);
    Valve valve(rho = rho);
    Pump booster(rho = rho, g = g, q(start = 0.06));
    Pipe delivery(rho = rho, g = g);
    output Real feedFlow(unit = "m3/s");
    output Real otherFlow(unit = "m3/s");
    output Real productFlow(unit = "m3/s");
    output Real suctionPressure(unit = "Pa");
    output Real headerPressure(unit = "Pa");
    output Real boosterDischarge(unit = "Pa");
    output Real feedHead(unit = "m");
    output Real feedHydraulic(unit = "W");
    output Real feedShaft(unit = "W");
    output Real boosterShaft(unit = "W");
    output Real massResidual(unit = "kg/s");
    output Real feedPressureResidual(unit = "Pa");
    output Real otherPressureResidual(unit = "Pa");
    output Real productPressureResidual(unit = "Pa");
    output Real energyResidual(unit = "W");
  equation
    connect(source.a, strainer.a);
    connect(strainer.b, feed.a);
    connect(feed.b, otherLine.a);
    connect(feed.b, processLine.a);
    connect(otherLine.b, otherSink.a);
    connect(processLine.b, valve.a);
    connect(valve.b, booster.a);
    connect(booster.b, delivery.a);
    connect(delivery.b, productSink.a);

    feedFlow = feed.q;
    otherFlow = otherLine.q;
    productFlow = booster.q;
    suctionPressure = feed.a.p;
    headerPressure = feed.b.p;
    boosterDischarge = booster.b.p;
    feedHead = feed.head;
    feedHydraulic = feed.hydraulic;
    feedShaft = feed.shaft;
    boosterShaft = booster.shaft;

    assert(feedFlow > 0 and otherFlow > 0 and productFlow > 0,
      "이 기준 회로에는 양의 순방향 유량이 필요합니다.");
    assert(suctionPressure > 0 and booster.a.p > 0, "펌프 흡입 절대압이 양수여야 합니다.");
    massResidual = rho * (feedFlow - otherFlow - productFlow);
    feedPressureResidual = headerPressure - suctionPressure - rho * g * feedHead;
    otherPressureResidual = headerPressure - otherSink.p - (otherLine.a.p - otherLine.b.p);
    productPressureResidual = headerPressure + rho * g * booster.head - productSink.p
      - (processLine.a.p - processLine.b.p) - (valve.a.p - valve.b.p)
      - (delivery.a.p - delivery.b.p);
    // 양쪽 펌프의 축 입력에서 효율 손실, 마찰 손실, 경계 압력 일과 위치 에너지를 뺀다.
    energyResidual = feedShaft + boosterShaft
      - feedShaft * (1 - feed.eta) - boosterShaft * (1 - booster.eta)
      - feedFlow * (strainer.a.p - strainer.b.p)
      - otherFlow * (otherLine.a.p - otherLine.b.p - rho * g * otherLine.dz)
      - productFlow * (processLine.a.p - processLine.b.p - rho * g * processLine.dz
          + valve.a.p - valve.b.p + delivery.a.p - delivery.b.p - rho * g * delivery.dz)
      - otherFlow * (otherSink.p - source.p + rho * g * otherLine.dz)
      - productFlow * (productSink.p - source.p + rho * g * (processLine.dz + delivery.dz));
  end Circuit;
end CduHydraulics;
