model Smoke
  Real x(start = 1, fixed = true);
equation
  der(x) = -x;
end Smoke;
