package PumpStation "PU-D4 직렬·병렬 펌프 묶음 요소. 펌프·배관·밸브는 PlantHydraulics 를 쓴다."
  import PlantHydraulics.*;

  model CheckValve "누설 없는 체크밸브: 순방향 R·q², 역방향 닫힘 (s 는 유량 또는 닫힌 압력차의 매개변수)"
    extends TwoPort;
    parameter Real resistance(unit = "Pa.s2/m6");
    Real s(start = 0.05);
  equation
    q = noEvent(if s > 0 then s else 0);
    a.p - b.p = noEvent(if s > 0 then resistance * s * s else s * 1e8);
  end CheckValve;

  model StoppedPump "정지한 펌프: 저항으로 통과. 0 이면 압력차 없음(병렬에서 체크밸브가 막는다)"
    extends TwoPort;
    parameter Real resistance(unit = "Pa.s2/m6") = 0;
  equation
    a.p - b.p = resistance * q * abs(q);
  end StoppedPump;
end PumpStation;
