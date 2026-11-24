package PlantHydraulics
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

  model Pump "양정 곡선 H(Q,N) 과 선택적 NPSH·캐비테이션 양정·효율 감소 (Python Network._solve_npsh 와 같은 식)"
    extends TwoPort;
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    parameter Real referenceRpm, rpm, eta;
    parameter Real ratedFlow(unit = "m3/s"), zeroHead(unit = "m"), ratedHead(unit = "m");
    parameter Boolean cavitation = false;
    parameter Real fluidTemp(unit = "K") = 1, refTemp(unit = "K") = 1, vaporRef(unit = "Pa") = 0, vaporB(unit = "K") = 0;
    parameter Real suctionDiameter(unit = "m") = 1, npshrRated(unit = "m") = 1, zeroFlowFraction = 0.5;
    parameter Real onsetMargin = 1.3, steep = 0, maxLoss = 0.5, effCoupling = 0;
    final parameter Real ratio = rpm / referenceRpm;
    Real head(unit = "m"), hydraulic(unit = "W"), shaft(unit = "W"), etaEff;
    Real vapor(unit = "Pa"), npsha(unit = "m"), npshr(unit = "m"), margin, x, loss;
  equation
    vapor = if cavitation then vaporRef * exp(vaporB * (1 / refTemp - 1 / fluidTemp)) else 0;
    npsha = if cavitation then (a.p - vapor) / (rho * g) + (q / (pi * suctionDiameter^2 / 4))^2 / (2 * g) else 0;
    npshr = if cavitation then ratio^2 * npshrRated * (zeroFlowFraction + (1 - zeroFlowFraction) * (q / (ratio * ratedFlow))^2) else 1;
    margin = if cavitation then npsha / npshr else onsetMargin + 1;
    x = (onsetMargin - margin) / (onsetMargin - 1);
    loss = noEvent(if x <= 0 then 0 elseif x <= 1 then 0.03 * x^2
                   else min(maxLoss, 0.03 + 0.06 * (x - 1) + steep * (x - 1)^2));
    head = (1 - loss) * (ratio^2 * zeroHead - (zeroHead - ratedHead) * (q / ratedFlow)^2);
    b.p - a.p = rho * g * head;
    hydraulic = q * (b.p - a.p);
    etaEff = eta * (1 - effCoupling * loss);
    shaft = hydraulic / etaEff;
  end Pump;

  model Circuit
    "입력 펌프 → 공통 헤더 → (다른 등가 분기 | 제품 분기: 배관 → 제어밸브 → 후속 펌프 흡입 스트레이너 → 후속 펌프 → 토출 배관)"
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    // 순방향 해 탐색 시작값. 설정의 설계 정격 유량(입력값)을 쓰며 Python 해를 넘기지 않는다.
    parameter Real feedStart(unit = "m3/s") = 0.11, otherStart(unit = "m3/s") = 0.05, productStart(unit = "m3/s") = 0.06;
    Boundary source, otherSink, productSink;
    Strainer strainer;
    Pump feed(rho = rho, g = g, q(start = feedStart));
    Pipe otherLine(rho = rho, g = g, q(start = otherStart));
    Pipe processLine(rho = rho, g = g);
    Valve valve(rho = rho);
    Strainer boosterStrainer;
    Pump booster(rho = rho, g = g, q(start = productStart));
    Pipe delivery(rho = rho, g = g);
    output Real feedFlow(unit = "m3/s");
    output Real otherFlow(unit = "m3/s");
    output Real productFlow(unit = "m3/s");
    output Real suctionPressure(unit = "Pa");
    output Real headerPressure(unit = "Pa");
    output Real boosterSuction(unit = "Pa");
    output Real boosterDischarge(unit = "Pa");
    output Real feedHead(unit = "m");
    output Real feedNpsha(unit = "m"), feedNpshr(unit = "m"), feedMargin, feedLoss;
    output Real boosterNpsha(unit = "m"), boosterNpshr(unit = "m"), boosterMargin, boosterLoss;
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
    connect(valve.b, boosterStrainer.a);
    connect(boosterStrainer.b, booster.a);
    connect(booster.b, delivery.a);
    connect(delivery.b, productSink.a);

    feedFlow = feed.q;
    otherFlow = otherLine.q;
    productFlow = booster.q;
    suctionPressure = feed.a.p;
    headerPressure = feed.b.p;
    boosterSuction = booster.a.p;
    boosterDischarge = booster.b.p;
    feedHead = feed.head;
    feedNpsha = feed.npsha;
    feedNpshr = feed.npshr;
    feedMargin = feed.margin;
    feedLoss = feed.loss;
    boosterNpsha = booster.npsha;
    boosterNpshr = booster.npshr;
    boosterMargin = booster.margin;
    boosterLoss = booster.loss;
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
      - (boosterStrainer.a.p - boosterStrainer.b.p) - (delivery.a.p - delivery.b.p);
    // 양쪽 펌프의 축 입력에서 효율 손실, 마찰 손실, 경계 압력 일과 위치 에너지를 뺀다.
    energyResidual = feedShaft * feed.etaEff + boosterShaft * booster.etaEff
      - feedFlow * (strainer.a.p - strainer.b.p)
      - otherFlow * (otherLine.a.p - otherLine.b.p - rho * g * otherLine.dz)
      - productFlow * (processLine.a.p - processLine.b.p - rho * g * processLine.dz
          + valve.a.p - valve.b.p + boosterStrainer.a.p - boosterStrainer.b.p
          + delivery.a.p - delivery.b.p - rho * g * delivery.dz)
      - otherFlow * (otherSink.p - source.p + rho * g * otherLine.dz)
      - productFlow * (productSink.p - source.p + rho * g * (processLine.dz + delivery.dz));
  end Circuit;

  model Machine "압력 상승 곡선 Δp(Q,N) 과 선택적 캐비테이션 양정 감소 (Python simulator.physics_single 과 같은 식)"
    extends TwoPort;
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    parameter Real referenceRpm, rpm, eta;
    parameter Real ratedFlow(unit = "m3/s"), zeroRise(unit = "Pa"), ratedRise(unit = "Pa");
    parameter Boolean cavitation = false;
    parameter Real fluidTemp(unit = "K") = 1, refTemp(unit = "K") = 1, vaporRef(unit = "Pa") = 0, vaporB(unit = "K") = 0;
    parameter Real suctionDiameter(unit = "m") = 1, npshrRated(unit = "m") = 1, zeroFlowFraction = 0.5;
    parameter Real onsetMargin = 1.3, steep = 0, maxLoss = 0.5, effCoupling = 0;
    final parameter Real ratio = rpm / referenceRpm;
    Real rise(unit = "Pa"), hydraulic(unit = "W"), shaft(unit = "W"), etaEff;
    Real vapor(unit = "Pa"), npsha(unit = "m"), npshr(unit = "m"), margin, x, loss;
  equation
    vapor = if cavitation then vaporRef * exp(vaporB * (1 / refTemp - 1 / fluidTemp)) else 0;
    npsha = if cavitation then (a.p - vapor) / (rho * g) + (q / (pi * suctionDiameter^2 / 4))^2 / (2 * g) else 0;
    npshr = if cavitation then ratio^2 * npshrRated * (zeroFlowFraction + (1 - zeroFlowFraction) * (q / (ratio * ratedFlow))^2) else 1;
    margin = if cavitation then npsha / npshr else onsetMargin + 1;
    x = (onsetMargin - margin) / (onsetMargin - 1);
    loss = noEvent(if x <= 0 then 0 elseif x <= 1 then 0.03 * x^2
                   else min(maxLoss, 0.03 + 0.06 * (x - 1) + steep * (x - 1)^2));
    rise = (1 - loss) * (ratio^2 * zeroRise - (zeroRise - ratedRise) / ratedFlow^2 * q^2);
    b.p - a.p = rise;
    hydraulic = q * rise;
    etaEff = eta * (1 - effCoupling * loss);
    shaft = hydraulic / etaEff;
  end Machine;

  model SingleCircuit "흡입 경계 → 스트레이너 → 흡입 배관 → 펌프/팬 → 토출 배관 → 토출 경계"
    parameter Real rho(unit = "kg/m3"), g(unit = "m/s2");
    parameter Real qStart(unit = "m3/s") = 0.1 "설정의 설계 정격 유량(입력). Python 해를 넘기지 않는다.";
    Boundary source, sink;
    Strainer strainer;
    Pipe suctionLine(rho = rho, g = g, q(start = qStart));
    Machine machine(rho = rho, g = g);
    Pipe dischargeLine(rho = rho, g = g);
    output Real volumeFlow(unit = "m3/s"), inletPressure(unit = "Pa"), outletPressure(unit = "Pa"), rise(unit = "Pa");
    output Real hydraulic(unit = "W"), shaft(unit = "W"), npsha(unit = "m"), npshr(unit = "m"), margin, headLoss;
    output Real pressureResidual(unit = "Pa"), energyResidual(unit = "W");
  equation
    connect(source.a, strainer.a);
    connect(strainer.b, suctionLine.a);
    connect(suctionLine.b, machine.a);
    connect(machine.b, dischargeLine.a);
    connect(dischargeLine.b, sink.a);
    volumeFlow = machine.q;
    inletPressure = machine.a.p;
    outletPressure = machine.b.p;
    rise = machine.rise;
    hydraulic = machine.hydraulic;
    shaft = machine.shaft;
    npsha = machine.npsha;
    npshr = machine.npshr;
    margin = machine.margin;
    headLoss = machine.loss;
    assert(volumeFlow > 0 and inletPressure > 0, "순방향 유량과 양의 흡입 절대압이 필요합니다.");
    pressureResidual = outletPressure - (dischargeLine.a.p - dischargeLine.b.p) - sink.p;
    energyResidual = shaft * machine.etaEff
      - volumeFlow * (strainer.a.p - strainer.b.p + suctionLine.a.p - suctionLine.b.p + dischargeLine.a.p - dischargeLine.b.p)
      - volumeFlow * (sink.p - source.p);
  end SingleCircuit;
end PlantHydraulics;
